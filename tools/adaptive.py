"""
adaptive.py
실제 채점 트래픽 기반 HPA util 자동 조정
scaler.py(긴급 대응)와 병행 실행

동작:
  - 파드 로그 스트리밍으로 실제 응답시간 측정
  - 60초마다 p95 기준 SLO 달성률 평가
  - SLO 미달 → HPA util 5% 낮춤 (더 일찍 스케일)
  - SLO 여유 충분(+20% 이상) → HPA util 5% 올림 (비용 절감)
  - 안정 구간 연속 3번 → 조정 중단 (수렴)

SLO 기준 (채점 기준표):
  user/product: p95 ≤ 200ms
  stress:       p95 ≤ 1000ms

사용법: python adaptive.py
"""
import subprocess, threading, sys, time, json
from collections import deque

NAMESPACE  = "apdev"
APPS       = ["user", "product", "stress"]
INTERVAL   = 60      # 평가 주기 (초)
WINDOW_SEC = 60      # p95 계산 윈도우

SLO_MS = {"user": 200, "product": 200, "stress": 1000}

# util 조정 범위
UTIL_MIN  = {"user": 40,  "product": 40,  "stress": 30}
UTIL_MAX  = {"user": 85,  "product": 85,  "stress": 70}
UTIL_STEP = 5  # 회당 조정폭 (%)

_buf     = {}    # pod → deque of (ts, dur_ms, status)
_pod_app = {}    # pod → app
_lock    = threading.Lock()


def kubectl(cmd):
    r = subprocess.run(f"kubectl {cmd}", shell=True, capture_output=True, text=True)
    return r.returncode == 0, r.stdout.strip()


def get_current_util(app):
    """현재 HPA targetUtilization 조회"""
    ok, out = kubectl(
        f'-n {NAMESPACE} get hpa/{app}-hpa -o '
        f'jsonpath="{{.spec.metrics[0].resource.target.averageUtilization}}"')
    try:
        return int(out.strip('"')) if ok else 70
    except Exception:
        return 70


def set_util(app, util):
    patch = json.dumps({"spec": {"metrics": [{"type": "Resource", "resource": {
        "name": "cpu", "target": {"type": "Utilization", "averageUtilization": util}
    }}]}}).replace('"', '\\"')
    ok, _ = kubectl(f'-n {NAMESPACE} patch hpa/{app}-hpa --type=merge -p "{patch}"')
    return ok


def get_pods(app):
    ok, out = kubectl(f'-n {NAMESPACE} get pods -l app={app} --no-headers '
                      f'-o custom-columns=NAME:.metadata.name')
    if not ok or not out:
        return []
    return [p.strip() for p in out.splitlines() if p.strip()]


def stream_pod(pod, app):
    with _lock:
        _buf[pod] = deque()
        _pod_app[pod] = app
    proc = subprocess.Popen(
        ["kubectl", "logs", "-f", "--tail=0", pod, "-n", NAMESPACE],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    for line in proc.stdout:
        try:
            s = line.find('{')
            if s < 0: continue
            d = json.loads(line[s:])
            status = d.get("status") or d.get("status_code")
            dur    = d.get("dur_ms") or d.get("duration_ms") or d.get("latency_ms")
            if status is None or dur is None or "/healthcheck" in d.get("path", ""):
                continue
            with _lock:
                _buf[pod].append((time.time(), float(dur), int(status)))
        except Exception:
            pass
    with _lock:
        _buf.pop(pod, None)
        _pod_app.pop(pod, None)


def watch_pods():
    active = set()
    while True:
        for app in APPS:
            for pod in get_pods(app):
                if pod not in active:
                    active.add(pod)
                    threading.Thread(target=stream_pod, args=(pod, app), daemon=True).start()
        time.sleep(15)


def p95(values):
    if not values: return 0
    s = sorted(values)
    return s[min(int(len(s) * 0.95), len(s) - 1)]


def main():
    print("=== Adaptive Tuner (SLO 기반 HPA util 자동 조정) ===")
    print(f"평가 주기: {INTERVAL}s | SLO: user/product≤200ms, stress≤1000ms\n")

    threading.Thread(target=watch_pods, daemon=True).start()
    time.sleep(3)

    stable_count = {app: 0 for app in APPS}  # 연속 안정 횟수
    frozen       = {app: False for app in APPS}  # 수렴 완료 여부

    print(f"  {'앱':<10} {'p95':>7} {'SLO달성':>8} {'현재util':>8} {'조정':>8}  상태")
    print(f"  {'─'*60}")

    while True:
        time.sleep(INTERVAL)
        cutoff = time.time() - WINDOW_SEC

        with _lock:
            for pod in list(_buf):
                while _buf[pod] and _buf[pod][0][0] < cutoff:
                    _buf[pod].popleft()
            app_data = {app: [] for app in APPS}
            for pod, entries in _buf.items():
                app = _pod_app.get(pod)
                if app:
                    app_data[app].extend(list(entries))

        print(f"\n[{time.strftime('%H:%M:%S')}]")
        for app in APPS:
            data = app_data[app]
            if not data:
                print(f"  {app:<10} {'N/A':>7} {'N/A':>8} {'':>8}  (트래픽 없음)")
                continue

            times   = [d[1] for d in data]
            p95_ms  = p95(times)
            slo_ms  = SLO_MS[app]
            slo_ok  = p95_ms <= slo_ms
            margin  = (slo_ms - p95_ms) / slo_ms  # 여유 비율 (음수면 초과)
            cur_util = get_current_util(app)

            if frozen[app]:
                print(f"  {app:<10} {p95_ms:>6.0f}ms {slo_ok and '✓' or '✗':>8} {cur_util:>7}%  수렴 완료")
                continue

            action = "-"
            new_util = cur_util

            if not slo_ok:
                # SLO 미달 → util 낮춰서 더 일찍 스케일
                new_util = max(UTIL_MIN[app], cur_util - UTIL_STEP)
                stable_count[app] = 0
                action = f"↓{new_util}%"
            elif margin >= 0.20:
                # 여유 20% 이상 → util 올려서 불필요 파드 감소
                new_util = min(UTIL_MAX[app], cur_util + UTIL_STEP)
                action = f"↑{new_util}%"
            else:
                # 안정 구간
                stable_count[app] += 1
                action = f"안정({stable_count[app]}/3)"
                if stable_count[app] >= 3:
                    frozen[app] = True
                    action = "→ 수렴!"

            if new_util != cur_util:
                ok = set_util(app, new_util)
                action += f" {'✓' if ok else '✗'}"

            slo_str = f"{'✓' if slo_ok else '✗'} {100*(1-abs(margin)):.0f}%"
            print(f"  {app:<10} {p95_ms:>6.0f}ms {slo_str:>8} {cur_util:>7}%  {action}")

        # 전체 수렴 완료 확인
        if all(frozen.values()):
            print(f"\n✓ 전체 앱 수렴 완료. 최적 util 고정됨.")
            for app in APPS:
                print(f"  {app}: {get_current_util(app)}%")
            print("\nadaptive 종료 (수렴 완료). scaler.py는 계속 실행하세요.")
            break


if __name__ == "__main__":
    main()
