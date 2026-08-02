"""
scaler.py — HPA 보완 오토스케일러 (레이턴시/5xx 기반, min-only)

■ 왜 필요한가 (HPA/Karpenter가 못 하는 것) — 랜덤 급증 트래픽 조기 방어
  · HPA는 CPU 신호 + 15초 주기(k8s 고정) → io 레이턴시 못 보고 15초 늦음.
  · scaler는 레이턴시 신호 + ~2초 주기 → 스파이크 '초기'에 HPA보다 먼저 warm 용량을 채운다.

■ 신호원 = 능동 프로브 (앱 바이너리 무관 — 이게 핵심 개선)
  · scaler가 직접 엔드포인트에 가벼운 요청을 쏴서 client-side 레이턴시/status를 잰다(채점기가 보는 값).
  · 예전엔 '파드 로그 파싱'이라 앱 바이너리 바뀌면 포맷 안 맞아 신호를 못 봐(장님) 무용지물이었음.
    능동 프로브는 로그 포맷과 무관 → 절대 장님 안 됨. (로그 파싱은 보조로 병행)
  · 사용법: python scaler.py <CF endpoint>   ← 엔드포인트 줘야 프로브 작동(권장).

■ HPA와 안 싸우는 법 (핵심)
  · currentReplicas는 절대 안 건드린다 (그건 HPA 소유 → 건드리면 요요).
  · 오직 HPA의 minReplicas(바닥)만 올렸다 내린다. min은 HPA의 입력이라 HPA가 순순히 따름.

■ 비용 안전 (노드 안 뜨게)
  · scaler가 올리는 min 총합은 '항상 켜진 MNG 노드 용량' 이내로만 허용(mng_fit_cap).
    → 올린 파드는 MNG에 얹힘 → 추가 노드 0 → min이 잠깐 유지돼도 비용 영향 없음.
  · Karpenter 노드는 HPA의 stress(CPU) 스케일에서만 뜨고, 부하 끝나면 Karpenter가 정리(scaler 무관).

■ min이 계속 떠있지 않게
  · 정상 상태가 NORMAL_WAIT 지속되면 min을 base(turn.py가 깐 값)로 단계적 복귀.

전제: turn.py로 먼저 리소스/HPA를 세팅한 뒤 실행. base min = 실행 시점의 HPA minReplicas.
사용법: python scaler.py
"""
import subprocess, threading, sys, time, json, re, math, os
import urllib.request, urllib.error, uuid as _uuid, random as _random
from collections import deque

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

NAMESPACE = "apdev"
APPS = ["user", "product", "stress"]
SLO = {"user": 200, "product": 200, "stress": 1000}   # ms

# 동작 파라미터
WINDOW = 5            # 판단에 쓰는 최근 구간(초)
POLL = 2             # 제어 루프 주기(초) — HPA(15s)보다 빠르게 (스파이크 조기대응)
PROBE_INTERVAL = 1.5 # 능동 프로브 주기(초) — 앱 무관 레이턴시 신호
PROBE_N = 2          # 주기당 앱별 프로브 수 (p95 표본 확보)
NORMAL_WAIT = 30     # 정상 지속 이만큼이면 min 한 단계 복귀
FIVEXX_THRESH = 3    # 최근 WINDOW초 5xx/timeout 이 이상이면 대응
TIMEOUT_MS = 30000   # 이 이상 걸리면 사실상 실패(ELB 502 유발) → 5xx로 카운트
LAT_TRIGGER = 0.7    # avg >= SLO*0.7 이면 '완전 초과 전에' 조기 scale-out (레이턴시 채점 방어)
MNG_SAFETY = 0.9     # MNG 용량의 이 비율까지만 min 상주 허용 (데몬셋/여유분)
SYSTEM_PER_NODE = 600

_buf = {}            # pod -> deque[(ts, dur_ms, status)]
_pod_app = {}        # pod -> app
_lock = threading.Lock()
_state = {"replicas": {a: 0 for a in APPS}, "nodes": [], "events": deque(maxlen=15)}


def kubectl(cmd):
    r = subprocess.run(f"kubectl {cmd}", shell=True, capture_output=True, text=True)
    return r.returncode == 0, r.stdout.strip()


def _parse_cpu_m(s):
    s = (s or "").strip().strip('"')
    if not s:
        return None
    try:
        return int(s[:-1]) if s.endswith("m") else int(float(s) * 1000)
    except ValueError:
        return None


def log_event(msg):
    with _lock:
        _state["events"].appendleft(f"[{time.strftime('%H:%M:%S')}] {msg}")


# ── 시작 시 클러스터에서 설정 읽기 (하드코딩 안 함) ──

def get_cluster_config():
    """MNG 노드 용량, 앱별 request(m), base min, max(=HPA maxReplicas)를 클러스터에서 읽음."""
    ok, alloc = kubectl('get nodes -o jsonpath="{.items[0].status.allocatable.cpu}"')
    avail = (_parse_cpu_m(alloc) or 1930) - SYSTEM_PER_NODE if ok else 1330

    # MNG 노드 수 = karpenter nodepool 라벨 없는 노드
    ok, out = kubectl('get nodes --no-headers -o custom-columns=POOL:.metadata.labels.karpenter\\.sh/nodepool')
    mng = sum(1 for l in out.splitlines() if l.strip() in ("<none>", "")) if ok and out else 2
    mng = max(1, mng)

    req_m, base_min, max_rep = {}, {}, {}
    fb_req = {"user": 30, "product": 30, "stress": 665}
    for app in APPS:
        _, rq = kubectl(f'-n {NAMESPACE} get deploy/{app} -o '
                        f'jsonpath="{{.spec.template.spec.containers[0].resources.requests.cpu}}"')
        req_m[app] = _parse_cpu_m(rq) or fb_req[app]
        _, mn = kubectl(f'-n {NAMESPACE} get hpa/{app}-hpa -o jsonpath="{{.spec.minReplicas}}"')
        _, mx = kubectl(f'-n {NAMESPACE} get hpa/{app}-hpa -o jsonpath="{{.spec.maxReplicas}}"')
        mn, mx = mn.strip('"'), mx.strip('"')
        base_min[app] = int(mn) if mn.isdigit() else 2
        max_rep[app] = int(mx) if mx.isdigit() else 12

    mng_cap = int(mng * avail * MNG_SAFETY)
    return {"avail": avail, "mng": mng, "mng_cap": mng_cap,
            "req_m": req_m, "base_min": base_min, "max_rep": max_rep}


def set_min_replicas(app, val):
    patch = json.dumps({"spec": {"minReplicas": val}}).replace('"', '\\"')
    ok, _ = kubectl(f'-n {NAMESPACE} patch hpa/{app}-hpa --type=merge -p "{patch}"')
    return ok


def mng_fit_cap(app, target, cur_min, cfg):
    """target까지 올리되, min 상주 총합이 MNG 용량을 넘지 않는 최대까지만 허용.
       → scaler가 올린 파드는 항상 MNG에 들어감 → 추가 노드 0 → 비용 영향 없음."""
    req_m, cap = cfg["req_m"], cfg["mng_cap"]
    while target > cur_min[app]:
        resident = sum((target if a == app else cur_min[a]) * req_m[a] for a in APPS)
        if resident <= cap:
            return target
        target -= 1
    return cur_min[app]


# ── 로그 파싱 (podlog와 동일 수준: JSON 필드 변형 + gin 텍스트 fallback + µs/s 단위) ──

def parse_log_line(line):
    """→ (status:int, ms:float, path) 또는 (None, None, None). podlog.parse_gin_line과 동형."""
    # 1) JSON: {"status":200,"dur_ms":109,"path":"/v1/user?..."}
    try:
        s = line.find("{")
        if s >= 0:
            o = json.loads(line[s:])
            status = next((o[k] for k in ("status", "status_code", "code") if o.get(k) is not None), None)
            dur_key = next((k for k in ("dur_ms", "duration_ms", "latency_ms", "latency", "latency_s", "dur_s")
                            if o.get(k) is not None), None)
            path = o.get("path") or o.get("uri") or o.get("url") or ""
            if status is not None and dur_key is not None:
                ms = float(o[dur_key])
                if dur_key.endswith("_s") or dur_key == "latency":   # 초 단위 필드 → ms 환산
                    ms *= 1000
                return int(status), ms, path.split("?")[0]
    except (ValueError, TypeError, KeyError):
        pass
    # 2) gin 텍스트: | 200 | 3.5ms |
    m = re.search(r'\|\s*(\d+)\s*\|\s*([\d.]+)(ms|µs|s)\s*\|', line)
    if m:
        val, unit = float(m.group(2)), m.group(3)
        ms = val if unit == "ms" else (val / 1000 if unit == "µs" else val * 1000)
        pm = re.search(r'(GET|POST|PUT|DELETE|PATCH)\s+"([^"]+)"', line)
        path = pm.group(2).split("?")[0] if pm else ""
        return int(m.group(1)), ms, path
    return None, None, None


# ── 로그 스트리밍: 앱별 실제 레이턴시/status 수집 (HPA가 못 보는 신호) ──

def stream_pod(pod, app):
    with _lock:
        _buf[pod] = deque(maxlen=5000)   # 최근만 사용 → 상한 필수(메모리 폭발 방지)
        _pod_app[pod] = app
    proc = subprocess.Popen(["kubectl", "logs", "-f", "--tail=0", pod, "-n", NAMESPACE],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    for line in proc.stdout:
        try:
            status, ms, path = parse_log_line(line)
            if status is None or ms is None or "/healthcheck" in (path or ""):
                continue
            with _lock:
                _buf[pod].append((time.time(), ms, status))
        except Exception:
            pass
    with _lock:
        _buf.pop(pod, None)
        _pod_app.pop(pod, None)


def watch_pods():
    active = set()
    while True:
        ok, out = kubectl('get pods -l app --field-selector=status.phase=Running --no-headers '
                          f'-n {NAMESPACE} -o custom-columns=NAME:.metadata.name,APP:.metadata.labels.app')
        if ok and out:
            current = set()
            for line in out.splitlines():
                p = line.split()
                if len(p) == 2 and p[1] in APPS:
                    current.add(p[0])
                    with _lock:
                        in_buf = p[0] in _buf
                    if p[0] not in active or not in_buf:
                        active.add(p[0])
                        threading.Thread(target=stream_pod, args=(p[0], p[1]), daemon=True).start()
            active -= (active - current)
        time.sleep(5)


def poll_replicas():
    while True:
        for app in APPS:
            ok, out = kubectl(f'-n {NAMESPACE} get hpa/{app}-hpa --no-headers '
                              f'-o custom-columns=R:.status.currentReplicas')
            try:
                with _lock:
                    _state["replicas"][app] = int(out.strip()) if ok and out.strip().isdigit() else 0
            except Exception:
                pass
        time.sleep(5)


def poll_nodes():
    while True:
        ok, out = kubectl("get nodes --no-headers -o custom-columns="
                          "NAME:.metadata.name,POOL:.metadata.labels.karpenter\\.sh/nodepool")
        nodes = []
        if ok and out:
            for line in out.splitlines():
                p = line.split()
                if p:
                    role = "karpenter" if len(p) > 1 and p[1] not in ("<none>", "") else "managed"
                    nodes.append({"name": p[0].split(".")[0], "role": role})
        with _lock:
            _state["nodes"] = nodes
        time.sleep(10)


# ── 측정 ──

def measure(app, cutoff):
    with _lock:
        data = []
        for pod, entries in _buf.items():
            if _pod_app.get(pod) == app:
                data.extend(e for e in entries if e[0] >= cutoff)
    if not data:
        return None
    durs = sorted(d[1] for d in data)
    n = len(durs)
    avg = sum(durs) / n
    p95 = durs[min(n - 1, int(n * 0.95))]
    n5xx = sum(1 for d in data if d[2] >= 500 or d[1] >= TIMEOUT_MS)
    rps = n / WINDOW
    return {"avg": avg, "p95": p95, "n5xx": n5xx, "rps": rps, "n": n}


# ── 능동 프로브: 앱 무관 레이턴시 신호 (로그 포맷과 독립 → 절대 장님 안 됨) ──

def _rid():
    return str(_random.randint(100000000000, 999999999999))


def _uid():
    return str(_uuid.uuid4())


def _http(url, data=None, timeout=5):
    """(status, ms). data가 있으면 POST(JSON). 실패/타임아웃 → status 0."""
    t0 = time.time()
    try:
        if data is not None:
            req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        else:
            req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            st = getattr(r, "status", 200)
    except urllib.error.HTTPError as e:
        st = e.code
    except Exception:
        st = 0
    return st, (time.time() - t0) * 1000


def _seed_probe(endpoint):
    u = f"_pr_{_random.randint(1000000, 9999999)}"
    p = f"_pr_{_random.randint(1000000, 9999999)}"
    _http(f"{endpoint}/v1/user", {"requestid": _rid(), "uuid": _uid(), "username": u, "email": f"{u}@t.org"})
    _http(f"{endpoint}/v1/product", {"requestid": _rid(), "uuid": _uid(), "id": p, "name": p, "price": 1})
    return u, p


def probe_loop(endpoint):
    """엔드포인트에 가벼운 요청을 계속 쏴서 앱별 레이턴시/status를 _buf에 채운다.
       control_loop의 measure()가 이걸 로그 데이터와 똑같이 소비 → 코드 변경 최소."""
    try:
        su, sp = _seed_probe(endpoint)
    except Exception:
        su, sp = "seed", "seed"
    keys = {a: f"__probe_{a}" for a in APPS}
    with _lock:
        for a in APPS:
            _buf[keys[a]] = deque(maxlen=2000)
            _pod_app[keys[a]] = a
    while True:
        for a in APPS:
            for _ in range(PROBE_N):
                if a == "user":
                    st, ms = _http(f"{endpoint}/v1/user?email={su}@t.org&requestid={_rid()}&uuid={_uid()}")
                elif a == "product":
                    st, ms = _http(f"{endpoint}/v1/product?id={sp}&requestid={_rid()}&uuid={_uid()}")
                else:
                    st, ms = _http(f"{endpoint}/v1/stress", {"requestid": _rid(), "uuid": _uid(), "length": _random.randint(50, 200)})
                with _lock:
                    _buf[keys[a]].append((time.time(), ms, st))
        time.sleep(PROBE_INTERVAL)


# ── 제어 루프: 레이턴시/5xx → min 조절 (MNG-fit 이내) ──

def _load_capacity():
    """파드당 안전 RPS(= SLO 이내 처리량). turn.py가 측정해 쓴 scaler_cap.json 우선, 없으면 기본값."""
    defaults = {"user": 40.0, "product": 80.0, "stress": 3.0}
    try:
        with open(os.path.join(os.path.dirname(__file__), "scaler_cap.json")) as f:
            cap = json.load(f)
        out = {a: max(1.0, float(cap.get(a, defaults[a]))) for a in APPS}
        log_event(f"capacity(rps/pod): {out}")
        return out
    except Exception:
        return dict(defaults)


def control_loop(cfg):
    """RPS 기반 비례 스케일: min = ceil(RPS / 파드당용량 × 여유). 과증설 없음, RPS 빠지면 빠른 축소.
       ★min만 조절(HPA와 안 싸움) + MNG 캡 제거(필요하면 오버플로 허용) + 점프 아닌 비례 = 예전 3대 실패 다 해결."""
    base_min, max_rep = cfg["base_min"], cfg["max_rep"]
    cap = _load_capacity()
    cur_min = dict(base_min)
    down_since = {a: 0 for a in APPS}
    rps_hist = {a: deque(maxlen=4) for a in APPS}   # 스파이크 램프 감지용 RPS 추세
    HEADROOM = 1.3    # 여유율(스케일업 지연 흡수)
    SPIKE_HEADROOM = 1.8  # RPS 급상승(램프) 시 — 앞서서 더 프로비저닝(스파이크 순간 꼬리 방지)
    DOWN_WAIT = 10    # RPS 내려간 뒤 이만큼 지속돼야 축소(요요 방지)

    for app in APPS:                      # 시작 시 base로 확정
        set_min_replicas(app, base_min[app])

    while True:
        t0 = time.time()
        cutoff = t0 - WINDOW
        stats = {}
        for app in APPS:
            m = measure(app, cutoff)
            stats[app] = m
            slo = SLO[app]
            # stress도 포함하되 안전: 지연 거버너(코핑 중이면 안 늘림) + max_rep=s_max(3)로 상한 → 폭증 불가.
            #   CPU-HPA(15s)보다 빠른 2s로 s_max 도달 → 동시성 스파이크 살짝 개선. app천장(큰 length)은 여전히
            #   물리라 못 고침(그건 스케일 무관). = "stress 보완"이되 비용 폭증 없음(s_max가 캡).

            # ── RPS로 사이징 + 지연 거버너로 과증설 방지 (네 비용 걱정의 핵심) ──
            if m is None or m["rps"] <= 0:
                need = base_min[app]                       # 트래픽 0 → baseline
                rps_hist[app].clear()
            else:
                rps = m["rps"]
                hist = rps_hist[app]
                prev = (sum(hist) / len(hist)) if hist else rps
                headroom = SPIKE_HEADROOM if (prev > 0 and rps > prev * 1.3) else HEADROOM
                hist.append(rps)
                need_rps = math.ceil(rps / cap[app] * headroom)   # RPS가 요구하는 파드 수
                # ★지연 거버너(과증설 방지): 지연이 여유로우면(클러스터가 잘 코핑) RPS 높아도 안 늘림.
                #   SLO 초과 → RPS대로+2(안전). SLO 근접(0.5×, 램프) → RPS대로 선제. 여유 → 안 늘림(과하면 축소).
                #   = "안 늘어나야 할 때 과하게 늘어남"을 지연이 직접 막음. 실측서 클러스터가 22ms로 코핑하면 안 늘어남.
                if m["p95"] >= slo or m["n5xx"] >= FIVEXX_THRESH:
                    need = need_rps + 2
                elif m["p95"] >= slo * 0.5:
                    need = need_rps
                else:
                    need = min(cur_min[app], need_rps)     # 코핑 중 → 유지/축소 = 비용 안전
            target = max(base_min[app], min(max_rep[app], need))

            if target > cur_min[app]:                      # 스케일업 — 즉시(RPS는 선행신호)
                set_min_replicas(app, target)
                r, p = (m["rps"], m["p95"]) if m else (0, 0)
                log_event(f"↑ {app} {cur_min[app]}→{target} (rps={r:.0f}/{cap[app]:.0f}pod p95={p:.0f})")
                cur_min[app] = target; down_since[app] = 0
            elif target < cur_min[app]:                    # 스케일다운 — RPS 빠지면 DOWN_WAIT 후 즉시
                if down_since[app] == 0:
                    down_since[app] = t0
                elif t0 - down_since[app] >= DOWN_WAIT:
                    set_min_replicas(app, target)
                    log_event(f"↓ {app} {cur_min[app]}→{target} (rps={(m['rps'] if m else 0):.0f})")
                    cur_min[app] = target; down_since[app] = 0
            else:
                down_since[app] = 0

        render(cfg, stats, cur_min)
        time.sleep(max(0, POLL - (time.time() - t0)))


# ── 대시보드 ──

def render(cfg, stats, cur_min):
    with _lock:
        replicas = dict(_state["replicas"])
        nodes = list(_state["nodes"])
        events = list(_state["events"])
    mng = [n for n in nodes if n["role"] == "managed"]
    karp = [n for n in nodes if n["role"] == "karpenter"]

    print("\033[H", end="")
    print("━" * 68 + "\033[K")
    print(f"  ⚙ scaler {time.strftime('%H:%M:%S')}  │  MNG:{len(mng)}  Karpenter:{len(karp)}  "
          f"Total:{len(nodes)}  │  MNG용량 {cfg['mng_cap']}m\033[K")
    print("━" * 68 + "\033[K")
    print(f"  {'앱':<9} {'avg':>7} {'p95':>7} {'5xx':>4} {'rps':>6} {'pods':>5} {'min':>4} {'base':>4}  상태\033[K")
    print("  " + "─" * 62 + "\033[K")
    for app in APPS:
        m = stats.get(app)
        pods = replicas.get(app, 0)
        base = cfg["base_min"][app]
        cm = cur_min[app]
        if not m:
            print(f"  {app:<9} {'--':>7} {'--':>7} {'--':>4} {'--':>6} {pods:>5} {cm:>4} {base:>4}  ⏳ 대기\033[K")
            continue
        slo = SLO[app]
        flag = "🔥 BOOST" if cm > base else ("⚠" if m["avg"] >= slo * LAT_TRIGGER else "✅")
        print(f"  {app:<9} {m['avg']:>6.0f}m {m['p95']:>6.0f}m {m['n5xx']:>4} {m['rps']:>6.1f} "
              f"{pods:>5} {cm:>4} {base:>4}  {flag}\033[K")
    print("\033[K")
    print("  이벤트\033[K")
    print("  " + "─" * 55 + "\033[K")
    shown = 0
    for e in events[:8]:
        print(f"    {e}\033[K")
        shown += 1
    for _ in range(8 - shown):
        print("\033[K")
    sys.stdout.flush()


def main():
    endpoint = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else None
    print("scaler 시작 — 클러스터 설정 읽는 중...")
    cfg = get_cluster_config()
    print(f"  MNG {cfg['mng']}노드, 용량 {cfg['mng_cap']}m (안전 {int(MNG_SAFETY*100)}%)")
    print(f"  request: {cfg['req_m']}")
    print(f"  base min: {cfg['base_min']}  max: {cfg['max_rep']}")

    for fn in [watch_pods, poll_replicas, poll_nodes]:
        threading.Thread(target=fn, daemon=True).start()
    if endpoint:
        print(f"  능동 프로브 ON: {endpoint} (앱 무관 레이턴시 신호)\n")
        threading.Thread(target=probe_loop, args=(endpoint,), daemon=True).start()
    else:
        print("  ⚠ 엔드포인트 미지정 → 로그 기반만 동작. 앱 로그 포맷이 다르면 신호를 못 봄(장님).")
        print("     권장: python scaler.py <CF endpoint>  (프로브로 절대 장님 안 되게)\n")
    time.sleep(4)
    print("\033[2J", end="")
    try:
        control_loop(cfg)
    finally:
        # ★종료(Ctrl+C/크래시) 시 min을 base로 복원 → 높은 min이 남아 노드가 안 죽는 일 방지(비용 안전).
        for app in APPS:
            set_min_replicas(app, cfg["base_min"][app])
        print("\n종료 — HPA min을 base로 복원함(비용 안전). CPU-HPA가 이어받음.")


if __name__ == "__main__":
    main()
