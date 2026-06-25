"""
scaler.py - 파드별 응답시간/5xx 기반 보조 스케일러
사용법: python scaler.py
"""
import subprocess, threading, sys, time, json
from collections import deque

NAMESPACE  = "apdev"
APPS       = ["user", "product", "stress"]
WINDOW_SEC = 30

THRESHOLDS = {
    "user":    {"avg_ms": 500,  "5xx": 5},
    "product": {"avg_ms": 500,  "5xx": 5},
    "stress":  {"avg_ms": 1300, "5xx": 5},
}
POD_THRESHOLDS = {
    "user":    {"avg_ms": 800,  "5xx": 10},
    "product": {"avg_ms": 800,  "5xx": 10},
    "stress":  {"avg_ms": 2000, "5xx": 10},
}
MIN_BASE  = {"user": 1, "product": 1, "stress": 1}
MIN_BOOST = {"user": 3, "product": 3, "stress": 5}

# 공유 상태 (백그라운드 스레드가 갱신)
_state = {
    "replicas": {app: 0 for app in APPS},   # HPA currentReplicas
    "nodes": [],                              # [{"name":..,"type":..,"role":..}]
    "events": deque(maxlen=20),              # 이벤트 로그
}
_buf      = {}    # pod → deque of (ts, dur_ms, status)
_pod_app  = {}    # pod → app
_lock     = threading.Lock()
_restarting = set()
_boosted    = {app: False for app in APPS}


def kubectl(cmd):
    r = subprocess.run(f"kubectl {cmd}", shell=True, capture_output=True, text=True)
    return r.returncode == 0, r.stdout.strip()


def log_event(msg):
    ts = time.strftime("%H:%M:%S")
    with _lock:
        _state["events"].appendleft(f"[{ts}] {msg}")


# ── 백그라운드: HPA replicas 갱신 (5초 주기) ──
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


# ── 백그라운드: 노드 목록 갱신 (10초 주기) ──
def poll_nodes():
    while True:
        ok, out = kubectl("get nodes --no-headers "
                          "-o custom-columns=NAME:.metadata.name,"
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


# ── 백그라운드: 파드 스트리밍 ──
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
        ok, out = kubectl(f'-n {NAMESPACE} get pods -l app '
                          f'--no-headers -o custom-columns='
                          f'NAME:.metadata.name,APP:.metadata.labels.app')
        if ok and out:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) == 2:
                    pod, app = parts
                    if pod not in active and app in APPS:
                        active.add(pod)
                        threading.Thread(target=stream_pod, args=(pod, app), daemon=True).start()
        time.sleep(15)


# ── 메인 루프 ──
def main():
    for fn in [poll_replicas, poll_nodes, watch_pods]:
        threading.Thread(target=fn, daemon=True).start()
    time.sleep(2)

    print("\033[2J", end="")  # clear screen

    while True:
        t0 = time.time()
        cutoff = t0 - WINDOW_SEC

        with _lock:
            for pod in list(_buf):
                while _buf[pod] and _buf[pod][0][0] < cutoff:
                    _buf[pod].popleft()
            pod_snap = {p: (a, list(_buf[p])) for p, a in _pod_app.items()}
            replicas = dict(_state["replicas"])
            nodes    = list(_state["nodes"])
            events   = list(_state["events"])

        # Pending 파드 감지 → 동일 앱 오래된 파드 삭제로 자리 확보
        ok, pending_out = kubectl(f'-n {NAMESPACE} get pods --field-selector=status.phase=Pending '
                                  f'--no-headers -o custom-columns=NAME:.metadata.name,APP:.metadata.labels.app')
        if ok and pending_out:
            for line in pending_out.splitlines():
                parts = line.split()
                if len(parts) == 2:
                    pending_pod, app = parts
                    if app not in APPS:
                        continue
                    # 해당 앱의 Running 파드 중 가장 오래된 것 삭제
                    ok2, running_out = kubectl(
                        f'-n {NAMESPACE} get pods -l app={app} --field-selector=status.phase=Running '
                        f'--no-headers --sort-by=.metadata.creationTimestamp '
                        f'-o custom-columns=NAME:.metadata.name')
                    if ok2 and running_out:
                        oldest = running_out.splitlines()[0].strip()
                        if oldest and oldest not in _restarting:
                            _restarting.add(oldest)
                            log_event(f"⚡ Pending {pending_pod[:25]} → {oldest[:25]} 교체")
                            def _evict(p=oldest):
                                kubectl(f'-n {NAMESPACE} delete pod {p} --grace-period=5')
                                time.sleep(35)
                                _restarting.discard(p)
                            threading.Thread(target=_evict, daemon=True).start()

        # 파드별 임계값 체크 → 문제 파드 삭제
        app_data = {app: [] for app in APPS}
        for pod, (app, data) in pod_snap.items():
            app_data[app].extend(data)
            if not data or pod in _restarting:
                continue
            avg_ms  = sum(d[1] for d in data) / len(data)
            cnt_5xx = sum(1 for d in data if d[2] >= 500)
            thr = POD_THRESHOLDS[app]
            if avg_ms >= thr["avg_ms"] or cnt_5xx >= thr["5xx"]:
                _restarting.add(pod)
                reason = ("avg↑" if avg_ms >= thr["avg_ms"] else "") + \
                         (" 5xx↑" if cnt_5xx >= thr["5xx"] else "")
                log_event(f"⚠ {pod[:30]} 재시작 [{reason.strip()}] avg={avg_ms:.0f}ms 5xx={cnt_5xx}")
                def _del(p=pod):
                    kubectl(f'-n {NAMESPACE} delete pod {p} --grace-period=5')
                    time.sleep(35)
                    _restarting.discard(p)
                threading.Thread(target=_del, daemon=True).start()

        # 앱 전체 minReplicas 보조
        for app in APPS:
            data = app_data[app]
            if not data:
                continue
            avg_ms  = sum(d[1] for d in data) / len(data)
            cnt_5xx = sum(1 for d in data if d[2] >= 500)
            thr = THRESHOLDS[app]
            breached = avg_ms >= thr["avg_ms"] or cnt_5xx >= thr["5xx"]
            if breached and not _boosted[app]:
                kubectl(f'-n {NAMESPACE} patch hpa/{app}-hpa --type=merge '
                        f'-p "{{\\"spec\\":{{\\"minReplicas\\":{MIN_BOOST[app]}}}}}"')
                _boosted[app] = True
                log_event(f"↑ {app} minReplicas→{MIN_BOOST[app]} (avg={avg_ms:.0f}ms 5xx={cnt_5xx})")
            elif not breached and _boosted[app]:
                kubectl(f'-n {NAMESPACE} patch hpa/{app}-hpa --type=merge '
                        f'-p "{{\\"spec\\":{{\\"minReplicas\\":{MIN_BASE[app]}}}}}"')
                _boosted[app] = False
                log_event(f"↓ {app} minReplicas→{MIN_BASE[app]} (정상 복귀)")

        # ── 출력 ──
        print("\033[H", end="")  # 커서 홈
        print(f"{'─'*65}")
        print(f"  Scaler  {time.strftime('%H:%M:%S')}   윈도우:{WINDOW_SEC}s")
        print(f"{'─'*65}")

        # 노드
        print(f"  {'NODES':}")
        managed = [n for n in nodes if n["role"] == "managed"]
        karp    = [n for n in nodes if n["role"] == "karpenter"]
        print(f"    MNG      : {len(managed)}대  " +
              " ".join(f"{n['name']}({n['type']})" for n in managed))
        print(f"    Karpenter: {len(karp)}대  " +
              (" ".join(f"{n['name']}({n['type']})" for n in karp) or "-"))
        print()

        # 앱별 메트릭
        print(f"  {'앱':<10} {'avg':>7} {'5xx':>5} {'pods':>5}  상태")
        print(f"  {'─'*45}")
        for app in APPS:
            data = app_data[app]
            cur  = replicas[app]
            if not data:
                print(f"  {app:<10} {'N/A':>7} {'N/A':>5} {cur:>5}  (트래픽 없음)\033[K")
                continue
            avg_ms  = sum(d[1] for d in data) / len(data)
            cnt_5xx = sum(1 for d in data if d[2] >= 500)
            thr = THRESHOLDS[app]
            breached = avg_ms >= thr["avg_ms"] or cnt_5xx >= thr["5xx"]
            flag = "⚠ BOOST" if _boosted[app] else ("⚠ 임계초과" if breached else "✓ ok")
            print(f"  {app:<10} {avg_ms:>6.0f}ms {cnt_5xx:>5} {cur:>5}  {flag}\033[K")
        print()

        # 이벤트 로그
        print(f"  이벤트 (최근 10건)")
        print(f"  {'─'*45}")
        for e in events[:10]:
            print(f"  {e}\033[K")
        # 남은 줄 지우기
        for _ in range(10 - min(len(events), 10)):
            print(f"\033[K")

        sys.stdout.flush()
        elapsed = time.time() - t0
        time.sleep(max(0, 1 - elapsed))


if __name__ == "__main__":
    main()
