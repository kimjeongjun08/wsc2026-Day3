"""
scaler.py
파드별 로그 스트리밍 기반 통합 스케일러

[1초 주기] 긴급 대응:
  - 파드별 5xx/응답시간 → 임계 초과 파드 재시작
  - 앱 전체 임계 초과 → minReplicas 올려서 HPA 보조
  - Pending 파드 → 오래된 파드 교체로 자리 확보

[60초 주기] 장기 최적화 (adaptive):
  - p95 기반 SLO 달성률 평가
  - SLO 미달 → HPA util -5%, 여유 → util +5%
  - 안정 3회 연속 → 수렴 완료

사용법: python scaler.py
"""
import subprocess, threading, sys, time, json
from collections import deque

NAMESPACE  = "apdev"
APPS       = ["user", "product", "stress"]

# 긴급 대응 임계값 (30초 윈도우)
THRESHOLDS = {
    "user":    {"avg_ms": 300,  "5xx": 5},
    "product": {"avg_ms": 300,  "5xx": 5},
    "stress":  {"avg_ms": 1500, "5xx": 5},
}

# adaptive 임계값

_buf     = {}
_pod_app = {}
_lock    = threading.Lock()

_state = {
    "replicas": {app: 0 for app in APPS},
    "nodes":    [],
    "events":   deque(maxlen=20),
}

def kubectl(cmd):
    r = subprocess.run(f"kubectl {cmd}", shell=True, capture_output=True, text=True)
    return r.returncode == 0, r.stdout.strip()

def log_event(msg):
    with _lock:
        _state["events"].appendleft(f"[{time.strftime('%H:%M:%S')}] {msg}")

def set_min_replicas(app, val):
    patch = json.dumps({"spec": {"minReplicas": val}}).replace('"', '\\"')
    ok, _ = kubectl(f'-n {NAMESPACE} patch hpa/{app}-hpa --type=merge -p "{patch}"')
    return ok

def get_pods(app):
    ok, out = kubectl(f'-n {NAMESPACE} get pods -l app={app} --no-headers '
                      f'-o custom-columns=NAME:.metadata.name')
    return [p.strip() for p in out.splitlines() if p.strip()] if ok and out else []

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

def poll_replicas():
    while True:
        for app in APPS:
            ok, out = kubectl(f'-n {NAMESPACE} get hpa/{app}-hpa --no-headers '
                              f'-o custom-columns=R:.status.currentReplicas')
            try:
                with _lock:
                    _state["replicas"][app] = int(out.strip()) if ok else 0
            except Exception:
                pass
        time.sleep(5)

def poll_nodes():
    while True:
        ok, out = kubectl("get nodes --no-headers -o custom-columns="
                          "NAME:.metadata.name,"
                          "TYPE:.metadata.labels.node\\.kubernetes\\.io/instance-type,"
                          "POOL:.metadata.labels.karpenter\\.sh/nodepool")
        nodes = []
        if ok and out:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 3:
                    role = "karpenter" if parts[2] != "<none>" else "managed"
                    nodes.append({"name": parts[0].split(".")[0], "type": parts[1], "role": role})
        with _lock:
            _state["nodes"] = nodes
        time.sleep(10)

def watch_pods():
    active = set()
    while True:
        ok, out = kubectl(f'-n {NAMESPACE} get pods -l app --no-headers '
                          f'-o custom-columns=NAME:.metadata.name,APP:.metadata.labels.app')
        if ok and out:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] in APPS and parts[0] not in active:
                    active.add(parts[0])
                    threading.Thread(target=stream_pod, args=(parts[0], parts[1]), daemon=True).start()
        time.sleep(5)

def p95(values):
    if not values: return 0
    s = sorted(values)
    return s[min(int(len(s) * 0.95), len(s) - 1)]

# ── adaptive 루프 (60초 주기) ──
def scale_loop():
    """30초마다: 즉각 scale (응답/5xx 감지) + 카펜터 노드 정리"""
    # 즉각 scale 임계값 (30초 윈도우)
    SCALE_THRESHOLDS = {
        "user":    {"avg_ms": 300, "5xx": 5},
        "product": {"avg_ms": 300, "5xx": 5},
        "stress":  {"5xx": 10},  # stress는 5xx만
    }
    last_scaled = {app: 0 for app in APPS}  # 쿨다운: 마지막 scale 시각

    time.sleep(30)

    while True:
        cutoff = time.time() - 30
        with _lock:
            app_data = {app: [] for app in APPS}
            for pod, entries in _buf.items():
                app = _pod_app.get(pod)
                if app:
                    app_data[app].extend([e for e in entries if e[0] >= cutoff])

        # 즉각 scale: 임계값 초과 시 replicas +2
        for app in APPS:
            data = app_data[app]
            if not data:
                continue
            # 쿨다운 60초
            if time.time() - last_scaled[app] < 30:
                continue

            avg_ms  = sum(d[1] for d in data) / len(data)
            cnt_5xx = sum(1 for d in data if d[2] >= 500)
            thr = SCALE_THRESHOLDS[app]

            breach = cnt_5xx >= thr.get("5xx", 999)
            if "avg_ms" in thr:
                breach = breach or avg_ms >= thr["avg_ms"]

            if breach:
                with _lock:
                    cur = _state["replicas"].get(app, 1)
                add = 5 if app == "stress" else 2
                new_replicas = cur + add
                ok, _ = kubectl(f"-n {NAMESPACE} scale deploy/{app} --replicas={new_replicas}")
                if ok:
                    last_scaled[app] = time.time()
                    log_event(f"⚡ {app} scale {cur}→{new_replicas} (avg={avg_ms:.0f}ms 5xx={cnt_5xx})")

        # 카펜터 노드 정리
        ok, karp_out = kubectl("get nodes -l karpenter.sh/nodepool --no-headers "
                               "-o custom-columns=NAME:.metadata.name,SCHED:.spec.unschedulable")
        if ok and karp_out:
            for line in karp_out.splitlines():
                parts = line.split()
                if not parts: continue
                knode = parts[0]
                if len(parts) > 1 and parts[1] == "true":
                    continue
                ok2, pods_out = kubectl(f"-n {NAMESPACE} get pods --field-selector spec.nodeName={knode} "
                                        f"--no-headers -o custom-columns=NAME:.metadata.name")
                if not ok2 or not pods_out or not pods_out.strip():
                    continue
                apdev_pods = [p.strip() for p in pods_out.splitlines() if p.strip()]
                if 0 < len(apdev_pods) <= 2:
                    kubectl(f"cordon {knode}")
                    for p in apdev_pods:
                        kubectl(f"-n {NAMESPACE} delete pod {p} --grace-period=10")
                    log_event(f"🔄 {knode.split('.')[0]} 파드→MNG ({len(apdev_pods)}개)")

        time.sleep(30)

# ── 메인 루프 (1초 주기) ──
def main():
    for fn in [poll_replicas, poll_nodes, watch_pods, scale_loop]:
        threading.Thread(target=fn, daemon=True).start()
    time.sleep(2)

    print("\033[2J", end="")

    while True:
        t0 = time.time()
        cutoff = t0 - 30  # 긴급 대응 윈도우

        with _lock:
            for pod in list(_buf):
                while _buf[pod] and _buf[pod][0][0] < cutoff:
                    _buf[pod].popleft()
            pod_snap  = {p: (a, list(_buf[p])) for p, a in _pod_app.items()}
            replicas  = dict(_state["replicas"])
            nodes     = list(_state["nodes"])
            events    = list(_state["events"])

        # 앱별 메트릭 집계 (파드 교체 없음 — HPA/Deployment가 관리)
        app_data = {app: [] for app in APPS}
        for pod, (app, data) in pod_snap.items():
            app_data[app].extend(data)



        # ── 출력 ──
        print("\033[H", end="")
        print(f"{'─'*65}")
        print(f"  Scaler  {time.strftime('%H:%M:%S')}   모니터링:1s | 스케일:30s")
        print(f"{'─'*65}")

        managed = [n for n in nodes if n["role"] == "managed"]
        karp    = [n for n in nodes if n["role"] == "karpenter"]
        print(f"  노드  MNG:{len(managed)}  Karpenter:{len(karp)}  "
              + " ".join(f"{n['name']}({n['type']})" for n in nodes))
        print()

        print(f"  {'앱':<10} {'avg':>7} {'5xx':>5} {'pods':>5}  상태")
        print(f"  {'─'*45}")
        for app in APPS:
            data = app_data[app]
            cur  = replicas[app]
            if not data:
                print(f"  {app:<10} {'N/A':>7} {'N/A':>5} {cur:>5}  트래픽 없음\033[K")
                continue
            avg_ms  = sum(d[1] for d in data) / len(data)
            cnt_5xx = sum(1 for d in data if d[2] >= 500)
            thr = THRESHOLDS[app]
            breached = avg_ms >= thr.get("avg_ms", 99999) or cnt_5xx >= thr.get("5xx", 999)
            flag = "⚠" if breached else "✓"
            print(f"  {app:<10} {avg_ms:>6.0f}ms {cnt_5xx:>5} {cur:>5}  {flag}\033[K")

        print()
        print(f"  이벤트")
        print(f"  {'─'*50}")
        for e in events[:8]:
            print(f"  {e}\033[K")
        for _ in range(8 - min(len(events), 8)):
            print(f"\033[K")

        sys.stdout.flush()
        elapsed = time.time() - t0
        time.sleep(max(0, 1 - elapsed))


if __name__ == "__main__":
    main()
