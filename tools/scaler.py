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
POD_THRESHOLDS = {
    "user":    {"avg_ms": 400,  "5xx": 10},
    "product": {"avg_ms": 400,  "5xx": 10},
    "stress":  {"avg_ms": 2000, "5xx": 10},
}
# adaptive 임계값
SLO_MS   = {"user": 200, "product": 200, "stress": 1000}
UTIL_MIN = {"user": 40,  "product": 40,  "stress": 30}
UTIL_MAX = {"user": 85,  "product": 85,  "stress": 70}

_buf     = {}
_pod_app = {}
_lock    = threading.Lock()

_state = {
    "replicas": {app: 0 for app in APPS},
    "nodes":    [],
    "events":   deque(maxlen=20),
}
_restarting = set()

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

def set_util(app, util):
    patch = json.dumps({"spec": {"metrics": [{"type": "Resource", "resource": {
        "name": "cpu", "target": {"type": "Utilization", "averageUtilization": util}
    }}]}}).replace('"', '\\"')
    ok, _ = kubectl(f'-n {NAMESPACE} patch hpa/{app}-hpa --type=merge -p "{patch}"')
    return ok

def get_current_util(app):
    ok, out = kubectl(f'-n {NAMESPACE} get hpa/{app}-hpa -o '
                      f'jsonpath="{{.spec.metrics[0].resource.target.averageUtilization}}"')
    try:
        return int(out.strip('"')) if ok else 70
    except Exception:
        return 70

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
        time.sleep(15)

def p95(values):
    if not values: return 0
    s = sorted(values)
    return s[min(int(len(s) * 0.95), len(s) - 1)]

# ── adaptive 루프 (60초 주기) ──
def adaptive_loop():
    stable_count = {app: 0 for app in APPS}
    frozen       = {app: False for app in APPS}
    time.sleep(60)  # 초기 데이터 수집 대기

    while True:
        cutoff = time.time() - 60
        with _lock:
            app_data = {app: [] for app in APPS}
            for pod, entries in _buf.items():
                app = _pod_app.get(pod)
                if app:
                    app_data[app].extend([e for e in entries if e[0] >= cutoff])

        for app in APPS:
            if frozen[app]:
                continue
            data = app_data[app]
            if not data:
                continue

            p95_ms   = p95([d[1] for d in data])
            slo_ms   = SLO_MS[app]
            margin   = (slo_ms - p95_ms) / slo_ms
            cur_util = get_current_util(app)

            if p95_ms > slo_ms:
                new_util = max(UTIL_MIN[app], cur_util - 5)
                stable_count[app] = 0
                if new_util != cur_util:
                    set_util(app, new_util)
                    log_event(f"adaptive ↓ {app} util {cur_util}→{new_util}% (p95={p95_ms:.0f}ms)")
            elif margin >= 0.20:
                new_util = min(UTIL_MAX[app], cur_util + 5)
                if new_util != cur_util:
                    set_util(app, new_util)
                    log_event(f"adaptive ↑ {app} util {cur_util}→{new_util}% (여유 {margin:.0%})")
            else:
                stable_count[app] += 1
                if stable_count[app] >= 3:
                    frozen[app] = True
                    log_event(f"adaptive ✓ {app} util={cur_util}% 수렴 완료")

        # 카펜터 노드에 앱 파드 1~2개만 남아있으면 cordon + evict → MNG로 이동 → 노드 자동 삭제
        ok, karp_out = kubectl("get nodes -l karpenter.sh/nodepool --no-headers "
                               "-o custom-columns=NAME:.metadata.name,SCHED:.spec.unschedulable")
        if ok and karp_out:
            for line in karp_out.splitlines():
                parts = line.split()
                if not parts: continue
                knode = parts[0]
                if len(parts) > 1 and parts[1] == "true":
                    continue  # 이미 cordoned
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

        time.sleep(60)

# ── 메인 루프 (1초 주기) ──
def main():
    for fn in [poll_replicas, poll_nodes, watch_pods, adaptive_loop]:
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

        # Pending 파드 → 오래된 파드 교체
        ok, pending_out = kubectl(f'-n {NAMESPACE} get pods --field-selector=status.phase=Pending '
                                  f'--no-headers -o custom-columns=NAME:.metadata.name,APP:.metadata.labels.app')
        if ok and pending_out:
            for line in pending_out.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] in APPS:
                    ok2, running_out = kubectl(
                        f'-n {NAMESPACE} get pods -l app={parts[1]} --field-selector=status.phase=Running '
                        f'--no-headers --sort-by=.metadata.creationTimestamp '
                        f'-o custom-columns=NAME:.metadata.name')
                    if ok2 and running_out:
                        oldest = running_out.splitlines()[0].strip()
                        if oldest and oldest not in _restarting:
                            _restarting.add(oldest)
                            log_event(f"⚡ Pending {parts[0][:20]} → {oldest[:20]} 교체")
                            def _evict(p=oldest):
                                kubectl(f'-n {NAMESPACE} delete pod {p} --grace-period=5')
                                time.sleep(35); _restarting.discard(p)
                            threading.Thread(target=_evict, daemon=True).start()

        # 파드별 메트릭 집계 + 우선순위 기반 교체
        # 우선순위: 5xx 많음 > avg 느림 > 오래된 파드
        app_data = {app: [] for app in APPS}
        pod_metrics = []  # (score, pod, app, avg_ms, cnt_5xx)

        for pod, (app, data) in pod_snap.items():
            app_data[app].extend(data)
            if pod in _restarting:
                continue
            if not data:
                continue
            avg_ms  = sum(d[1] for d in data) / len(data)
            cnt_5xx = sum(1 for d in data if d[2] >= 500)
            thr = POD_THRESHOLDS[app]
            # 임계값 초과한 파드만 후보 (높을수록 나쁜 파드)
            if avg_ms >= thr["avg_ms"] or cnt_5xx >= thr["5xx"]:
                score = cnt_5xx * 1000 + avg_ms  # 5xx 우선, 같으면 느린 것
                pod_metrics.append((score, pod, app, avg_ms, cnt_5xx))

        # 앱당 가장 나쁜 파드 1개씩만 교체 (한꺼번에 너무 많이 죽이지 않도록)
        replaced_apps = set()
        for score, pod, app, avg_ms, cnt_5xx in sorted(pod_metrics, reverse=True):
            if app in replaced_apps:
                continue
            replaced_apps.add(app)
            _restarting.add(pod)
            reason = (f"avg={avg_ms:.0f}ms" if avg_ms >= POD_THRESHOLDS[app]["avg_ms"] else "") + \
                     (f" 5xx={cnt_5xx}" if cnt_5xx >= POD_THRESHOLDS[app]["5xx"] else "")
            log_event(f"⚠ {pod[:28]} 교체 [{reason.strip()}]")
            def _del(p=pod):
                kubectl(f'-n {NAMESPACE} delete pod {p} --grace-period=10')
                time.sleep(35); _restarting.discard(p)
            threading.Thread(target=_del, daemon=True).start()



        # ── 출력 ──
        print("\033[H", end="")
        print(f"{'─'*65}")
        print(f"  Scaler+Adaptive  {time.strftime('%H:%M:%S')}   긴급:30s | 최적화:60s")
        print(f"{'─'*65}")

        managed = [n for n in nodes if n["role"] == "managed"]
        karp    = [n for n in nodes if n["role"] == "karpenter"]
        print(f"  노드  MNG:{len(managed)}  Karpenter:{len(karp)}  "
              + " ".join(f"{n['name']}({n['type']})" for n in nodes))
        print()

        print(f"  {'앱':<10} {'avg':>7} {'5xx':>5} {'pods':>5} {'util':>5}  상태")
        print(f"  {'─'*50}")
        for app in APPS:
            data = app_data[app]
            cur  = replicas[app]
            util = get_current_util(app)
            if not data:
                print(f"  {app:<10} {'N/A':>7} {'N/A':>5} {cur:>5} {util:>4}%  트래픽 없음\033[K")
                continue
            avg_ms  = sum(d[1] for d in data) / len(data)
            cnt_5xx = sum(1 for d in data if d[2] >= 500)
            thr = THRESHOLDS[app]
            breached = avg_ms >= thr["avg_ms"] or cnt_5xx >= thr["5xx"]
            flag = "⚠" if breached else "✓"
            print(f"  {app:<10} {avg_ms:>6.0f}ms {cnt_5xx:>5} {cur:>5} {util:>4}%  {flag}\033[K")

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
