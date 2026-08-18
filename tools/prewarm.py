"""
prewarm.py — Karpenter warm node 관리 (overprovisioning 동적 조절) · 자기완결형

■ 목적
  Karpenter엔 EC2 웜풀을 직접 못 붙이니, pause 파드로 "웜풀처럼" 동작하게 구현.
  stress 파드가 스케일할 때 노드 대기 시간을 0초로 만드는 게 핵심.

  평소:  stress 필요 → 노드 없음 → Karpenter가 EC2 생성(20~40s) → 배치
  prewarm: 수요↑ 감지 → pause 파드 올림 → Karpenter 노드 미리 확보
           → stress 필요 → pause를 preempt(즉시 축출) → 0초 배치
  idle:   replicas=0 → 노드 0 → 비용 0

■ 2026-08-13 개정 — 과증설/낭비 제거 (진단 반영)
  핵심 불변식: **총 워커노드 ≤ cap** 을 두 경로 모두에서 강제한다.
    · budget = cap - 전체워커노드(warm 포함)  ← 폴백 경로도 동일 정의로 통일(구 버전은
      cap - stress_nodes 라 이미 뜬 warm을 예산에서 안 빼 낙관적 과발주 → 노드가 샜다).
    · 재생성 루프로 노드가 늘면 다음 주기 budget이 줄어 warm을 자기교정으로 반납한다.
      → Deployment 재생성을 이벤트로 쫓지 않아도 총량이 구조적으로 cap에 묶인다.
  범위 축소: warm 노드는 role=worker(=stress 전용 Karpenter 노드)다. user/product는 MNG에
    패킹되어 이 노드를 쓸 수 없으므로 트리거에서 제외한다(구 버전의 오발동/낭비 원인).
    → 프로브도 stress만 → 부하도 줄고 신호도 정확.
  전제 검증: stress가 pause(-10)를 실제로 선점 가능한지 시작 시 확인(안 되면 경고).
  중복발주 억제: 실제 Running pause 수를 세서 '채우는 중'이면 추가 증설 보류.
  관측: 매 주기 전체노드/stress/warm/budget 을 로그로 남겨 '묶여 있음'을 눈으로 검증.

■ 자기완결형(terraform 파이프라인 무변경)
  시작 시 pause-priority PriorityClass + overprovisioning Deployment(replicas=0)를 스스로 apply.

■ 전제(wsc2026-Day3): stress가 required anti-affinity로 전용 role=worker 노드 사용(deploy.yaml).
  pause(app=overprovisioning, role=worker, priority -10)를 stress(고우선순위)가 preempt → warm 효과.

사용법: python prewarm.py <CF endpoint>
  scaler.py와 동시 실행 권장. scaler_state.json(수요·노드 회계)을 1차 신호로 쓰고,
  없거나 stale하면 자체 stress 프로브로 폴백(단독 실행 가능).
"""
import subprocess
import sys
import os
import time
import json
import urllib.request
import urllib.error
from collections import deque

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

NAMESPACE = "apdev"
DEPLOY_NAME = "overprovisioning"

# ── 파라미터 ──
POLL_INTERVAL = 2         # 판단 주기(초).
TREND_WINDOW = 3          # 최근 N개 샘플로 추세 판단.
RAMP_THRESHOLD = 1.35     # 상승 추세로 볼 배율. ★1.2→1.35: 20% 노이즈성 변동에 warm 뜨던 것 완화.
SPIKE_THRESHOLD = 1.6     # spike 임박 배율(+절대하한).
SUSTAIN_MIN = 2           # ★최근 TREND_WINDOW 표본 중 이만큼이 임계를 넘어야 발동(단발 노이즈 억제).
MIN_WARM_PRESSURE = 0.25  # 절대 하한(정규화 압박도). 이 밑이면 비율이 튀어도 warm 안 뜸.
ABS_HIT_PRESSURE = 0.5    # '지연이 SLO의 절반' = 절대 압박 신호.
BASE_RISE_MULT = 1.25     # 절대 임계 발동에 'baseline 압박도의 이 배수 이상'을 함께 요구(상시발동 차단).
CALM_BASE_MULT = 1.10     # 웜풀 반납(CALM)도 baseline 상대로 판정.
CALM_ABS = 0.4            # 절대적으로 이 밑이면 CALM.
COOLDOWN_WAIT = 30        # 하락 후 이만큼 지속돼야 warm 축소(플랩 방지).
# ↓ 아래 3개는 ★기본값(fallback) — turn.py가 저장한 prewarm_cfg.json이 있으면 main()에서 덮음.
MAX_WARM_NODES = 2        # warm 노드 상한(추가로 미리 띄우는 최대 노드 수). budget 클램프가 실질 방어.
WARM_SHORTFALL_MIN = 2    # ★warm은 '한 번에 이만큼+ 노드가 부족한 급증'에만 뜬다. 완만한 램프
                          #   (부족 ≤1)는 Karpenter가 순차 부팅으로 따라잡으므로 warm 불필요
                          #   (그게 램프 구간 과증설의 실제 원인이었다). 앱 무관 일반 원리.
KP_NODE_CAP = 7           # Karpenter 노드 예산(정적 폴백). 실시간은 nodepool/scaler_state에서 읽음.
STRESS_SLO = 1000         # ms. warm은 stress 전용이라 stress SLO만 본다.

# ── 자기완결형 부트스트랩: pause-priority + overprovisioning(replicas=0) ──
BOOTSTRAP_YAML = """apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: pause-priority
value: -10
globalDefault: false
description: "overprovisioning - 최저 우선순위. 실제 파드가 오면 preempt → warm node 효과"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: overprovisioning
  namespace: apdev
spec:
  replicas: 0
  selector:
    matchLabels:
      app: overprovisioning
  template:
    metadata:
      labels:
        app: overprovisioning
      annotations:
        karpenter.sh/do-not-disrupt: "true"
    spec:
      priorityClassName: pause-priority
      terminationGracePeriodSeconds: 0
      nodeSelector:
        role: worker
      containers:
        - name: pause
          image: registry.k8s.io/pause:3.9
          resources:
            requests:
              cpu: PAUSE_CPUm
              memory: 128Mi
            limits:
              cpu: PAUSE_CPUm
              memory: 128Mi
"""


def kubectl(cmd):
    r = subprocess.run(f"kubectl {cmd}", shell=True, capture_output=True, text=True)
    return r.returncode == 0, r.stdout.strip()


def _load_cfg():
    """turn.py가 저장한 노드-유도 값(pause_cpu, kp_node_cap, max_warm). 없으면 안전 기본값."""
    try:
        with open(os.path.join(os.path.dirname(__file__), "prewarm_cfg.json")) as f:
            c = json.load(f)
        _VCPU[0] = max(1, int(c.get("vcpu", 2)))
        return (int(c.get("pause_cpu", 1900)),
                max(1, int(c.get("kp_node_cap", 7))),
                # max_warm=0 허용(웜풀 완전 비활성). 투기적 노드는 곧 비용 손실이라 끌 수 있어야 함.
                max(0, int(c.get("max_warm", 2))))
    except Exception:
        return 1900, 7, 2


_VCPU = [2]   # _load_cfg에서 갱신 (get_live_kp_cap의 노드수 환산용)


def get_live_kp_cap():
    """현재 Karpenter NodePool limits.cpu → 노드 수. scaler 단계확장을 실시간 반영. 실패 시 None."""
    ok, out = kubectl('get nodepool apdev-pool -o jsonpath="{.spec.limits.cpu}"')
    if not ok or not out:
        return None
    try:
        return max(1, int(float(out.strip().strip('"'))) // max(1, _VCPU[0]))
    except (ValueError, TypeError):
        return None


def bootstrap(pause_cpu):
    """pause-priority + overprovisioning 배포를 직접 apply(idempotent). replicas=0으로 시작(비용 0)."""
    yaml = BOOTSTRAP_YAML.replace("PAUSE_CPU", str(pause_cpu))
    r = subprocess.run("kubectl apply -f -", shell=True, input=yaml,
                       capture_output=True, text=True)
    ok_pc, _ = kubectl("get priorityclass pause-priority -o name")
    ok_dp, _ = kubectl(f"-n {NAMESPACE} get deploy/{DEPLOY_NAME} -o name")
    if ok_pc and ok_dp:
        if r.returncode == 0:
            print(f"  overprovisioning + pause-priority 준비 완료 (replicas=0, pause={pause_cpu}m)")
        else:
            print(f"  overprovisioning + pause-priority 이미 존재 → 그대로 사용 (pause={pause_cpu}m)")
            print(f"    ※apply 일부 거부: {r.stderr.strip()[:120]}")
        # pause_cpu가 다르면 갱신(노드 독점 보장에 직결)
        _, cur = kubectl(f'-n {NAMESPACE} get deploy/{DEPLOY_NAME} '
                         '-o jsonpath="{.spec.template.spec.containers[0].resources.requests.cpu}"')
        cur = (cur or "").strip().strip('"')
        if cur and cur != f"{pause_cpu}m":
            patch = json.dumps({"spec": {"template": {"spec": {"containers": [
                {"name": "pause", "resources": {"requests": {"cpu": f"{pause_cpu}m"},
                                                "limits": {"cpu": f"{pause_cpu}m"}}}]}}}}).replace('"', '\\"')
            if kubectl(f'-n {NAMESPACE} patch deploy/{DEPLOY_NAME} --type=strategic -p "{patch}"')[0]:
                print(f"    pause CPU 갱신: {cur} → {pause_cpu}m (노드 독점 유지)")
    else:
        miss = []
        if not ok_pc:
            miss.append("PriorityClass pause-priority")
        if not ok_dp:
            miss.append(f"Deployment {DEPLOY_NAME}")
        print(f"  ⚠ 웜풀 사용 불가 — 없는 객체: {', '.join(miss)}")
        print(f"    apply 오류: {r.stderr.strip()[:200]}")


def verify_preempt():
    """★웜풀 전제 검증: stress 파드가 pause(-10)를 실제로 선점 가능한가.
      stress 파드의 priorityClassName과 그 값/preemptionPolicy를 읽어 확인한다.
        · priority 값 > -10  AND  preemptionPolicy != Never  → 선점 가능(정상)
        · 값이 없으면(default 0, PreemptLowerPriority) → 정상
      stress 파드가 아직 없으면 판정 보류(None) — 나중에 뜨면 재확인.
      실패해도 중단하지 않고 '경고'만 한다(웜풀이 무의미하게 노드만 띄우는 상황을 눈에 보이게)."""
    ok, pcname = kubectl(f'-n {NAMESPACE} get pods -l app=stress '
                         '-o jsonpath="{.items[0].spec.priorityClassName}"')
    if not ok:
        return None
    pcname = (pcname or "").strip().strip('"')
    if pcname == "":
        # 명시 PriorityClass 없음 → 기본 우선순위 0(> -10) + 기본정책 PreemptLowerPriority → 선점 가능
        return True
    _, val = kubectl(f'get priorityclass {pcname} -o jsonpath="{{.value}}"')
    _, pol = kubectl(f'get priorityclass {pcname} -o jsonpath="{{.preemptionPolicy}}"')
    try:
        v = int((val or "0").strip().strip('"'))
    except ValueError:
        v = 0
    pol = (pol or "").strip().strip('"') or "PreemptLowerPriority"
    return (v > -10) and (pol != "Never")


def set_replicas(n):
    patch = json.dumps({"spec": {"replicas": n}}).replace('"', '\\"')
    ok, _ = kubectl(f'-n {NAMESPACE} patch deploy/{DEPLOY_NAME} --type=merge -p "{patch}"')
    return ok


def get_current_replicas():
    ok, out = kubectl(f'-n {NAMESPACE} get deploy/{DEPLOY_NAME} -o jsonpath="{{.spec.replicas}}"')
    try:
        return int(out.strip().strip('"')) if ok else 0
    except ValueError:
        return 0


def get_running_pause_pods():
    """실제 Running pause 파드 수(= 실효 warm 노드). desired(spec.replicas)와 달라
      '재보충 중' 슬롯(desired - running)을 알 수 있어 중복 발주를 막는다."""
    ok, out = kubectl(f'-n {NAMESPACE} get pods -l app={DEPLOY_NAME} --no-headers '
                      f'--field-selector=status.phase=Running')
    if not ok or not out:
        return 0
    return len([l for l in out.splitlines() if l.strip()])


def get_stress_pods():
    """현재 Running stress 파드 수(= stress 노드 수, anti-affinity로 1파드=1노드)."""
    ok, out = kubectl(f'-n {NAMESPACE} get pods -l app=stress --no-headers '
                      f'--field-selector=status.phase=Running')
    if not ok or not out:
        return 0
    return len([l for l in out.splitlines() if l.strip()])


def get_live_worker_nodes():
    """살아있는 Karpenter(워커) 노드 수. 종료 중·등록 중은 제외."""
    ok, out = kubectl("get nodes -l karpenter.sh/nodepool --no-headers -o custom-columns="
                      "N:.metadata.name,ST:.status.conditions[-1].type,DEL:.metadata.deletionTimestamp")
    if not ok or not out:
        return 0
    n = 0
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 2 and p[1] == "Ready" and not (len(p) > 2 and p[2] not in ("<none>", "", "-")):
            n += 1
    return n


# ── 프로브: grader와 동일한 stress 요청으로 "바쁜 정도" 측정 (stress 전용) ──

def _http(url, data=None, timeout=5):
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


def probe_stress_pressure(endpoint):
    """★grader와 100% 동일: POST /v1/stress {"length":50}. stress 압박도(ms/SLO) 반환.
      - 200/201 → ms/STRESS_SLO
      - 5xx     → 2.0 (서버 과부하 = 스파이크 신호)
      - 그 외(4xx/WAF/네트워크/0) → None (표본 무시 = 안전 실패, 비용 폭주 방지)
    """
    st, ms = _http(f"{endpoint}/v1/stress", {"length": 50})
    if st in (200, 201):
        return ms / STRESS_SLO
    if 500 <= st <= 599:
        return 2.0
    return None


def read_scaler_state(max_age=15.0):
    """scaler가 발행한 수요 상태(scaler_state.json)를 읽는다. 이게 prewarm의 1차 신호(선행 지표)."""
    try:
        p = os.path.join(os.path.dirname(__file__), "scaler_state.json")
        with open(p) as f:
            s = json.load(f)
        if time.time() - float(s.get("ts", 0)) > max_age:
            return None
        return s
    except Exception:
        return None


def _median(vals):
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def clamp_warm(target, all_nodes, running_pause, current_warm, cap):
    """★과증설 방지의 핵심 클램프. 두 경로가 공통으로 쓴다.
      1) budget = cap - 전체워커노드(warm 포함) → 총 노드 ≤ cap 불변식.
      2) 재보충 중(running_pause < current_warm)이면 목표를 현재보다 올리지 않는다
         (이미 채우는 중인데 또 늘리면 이중 발주 → 노드 폭증의 직접 원인).
      반환: (desired, budget)"""
    budget = max(0, cap - all_nodes)
    desired = max(0, min(target, budget))
    if running_pause < current_warm and desired > current_warm:
        desired = current_warm      # 채우는 중 → 추가 상향 보류(감소는 허용)
    return desired, budget


def main():
    if len(sys.argv) < 2:
        print("사용법: python prewarm.py <CF endpoint>")
        sys.exit(1)

    global KP_NODE_CAP, MAX_WARM_NODES
    pause_cpu, KP_NODE_CAP, MAX_WARM_NODES = _load_cfg()

    endpoint = sys.argv[1].rstrip("/")
    print("=== prewarm.py (stress 전용 Karpenter warm node 관리) ===")
    print(f"  endpoint: {endpoint}")
    print(f"  warm 상한: {MAX_WARM_NODES}대 · 총노드 ≤ cap({KP_NODE_CAP}) 불변식 · pause={pause_cpu}m")
    bootstrap(pause_cpu)

    pre = verify_preempt()
    if pre is True:
        print("  preempt 전제 OK — stress가 pause(-10)를 선점 가능")
    elif pre is False:
        print("  ⚠ preempt 불가 가능성 — stress PriorityClass가 pause를 선점 못 할 수 있음")
        print("    → 이 경우 warm 노드는 자리만 차지(0초 배치 효과 없음). stress 우선순위/정책 확인 필요")
    else:
        print("  preempt 전제: stress 파드가 아직 없어 판정 보류(뜨면 재확인)")
    print("  동작: stress 수요 상승 → 노드 미리 확보 → stress가 preempt로 0초 배치")
    print("  ※ warm은 role=worker(stress 전용) 노드다. user/product(MNG 패킹)는 트리거에서 제외.\n")

    set_replicas(0)  # 초기 비용 0

    latency_history = []
    press_floor = deque(maxlen=90)   # 유휴 baseline 압박도 추정(약 3분)
    cooldown_start = 0
    pre_rechecked = (pre is not None)

    while True:
        try:
            live_cap = get_live_kp_cap()
            current_warm = get_current_replicas()
            running_pause = get_running_pause_pods()

            # stress가 나중에 떠서 preempt 전제를 아직 확인 못 했으면 1회 재확인
            if not pre_rechecked and get_stress_pods() > 0:
                r = verify_preempt()
                if r is False:
                    print("  ⚠ [재확인] stress가 pause를 선점 못 할 수 있음 — warm 효과 없음, 우선순위 점검")
                elif r is True:
                    print("  [재확인] preempt 전제 OK")
                pre_rechecked = True

            # ══ 1차 신호: scaler_state.json (선행 지표 + 노드 회계 공유) ═══════════════
            st = read_scaler_state()
            if st is not None:
                need_nodes = int(st.get("nodes_needed", 0))
                climbing = bool(st.get("climbing"))
                pend_total = sum(int(v) for v in (st.get("pending") or {}).values())
                # ★노드 회계는 scaler와 '같은 정의'를 쓴다(total_nodes = 전체 워커노드).
                #   구 버전이 폴백에서 cap-stress_nodes를 써 warm을 예산에 안 넣던 불일치를 제거.
                all_nodes = int(st.get("total_nodes", get_live_worker_nodes()))
                cap = live_cap or int(st.get("kp_cap", 0)) or KP_NODE_CAP

                shortfall = need_nodes - all_nodes
                if pend_total > 0:
                    # Karpenter가 이미 노드를 만드는 중 → warm이 더 빠를 수 없다. 추가 발주 금지.
                    target, reason = 0, f"PENDING×{pend_total}(카펜터 진행중)"
                elif climbing and shortfall >= WARM_SHORTFALL_MIN:
                    # ★'급증'일 때만 선확보한다: 수요가 한 번에 2노드+ 부족한 경우.
                    #   완만한 램프(부족 ≤1)는 Karpenter가 파드 하나씩 순차 부팅으로 이미 따라잡으므로
                    #   warm이 처리량에 기여하지 않고 비용만 낸다(= 램프 구간 과증설의 실제 원인).
                    #   여기서만 선확보하면 warm은 'Karpenter가 못 따라가는 점프'에만 뜬다(일반 원리).
                    target, reason = min(MAX_WARM_NODES, shortfall), f"급증 선확보(부족 {shortfall})"
                else:
                    # 완만 램프·플래토·안정 → warm 불필요(Karpenter가 따라잡음). idle엔 0 회수.
                    target, reason = 0, "완만/안정(warm 불필요)"

                desired, budget = clamp_warm(target, all_nodes, running_pause, current_warm, cap)
                _apply(desired, current_warm, reason, all_nodes,
                       get_stress_pods(), running_pause, budget, cap)
                time.sleep(POLL_INTERVAL)
                continue

            # ══ 폴백: scaler stale/부재 → 자체 stress 프로브 (단독 실행 지원) ═════════
            _busy = get_stress_pods() > 1 or current_warm > 0   # 부하 중이면 프로브 지연 회귀 방지용(현재는 항상 매주기)
            press = probe_stress_pressure(endpoint)             # stress 압박도(1.0 = SLO 도달)
            if press is None:
                time.sleep(POLL_INTERVAL)
                continue
            latency_history.append(press)
            if len(latency_history) > TREND_WINDOW * 3:
                latency_history = latency_history[-TREND_WINDOW * 3:]
            if len(latency_history) < TREND_WINDOW:
                time.sleep(POLL_INTERVAL)
                continue

            recent = latency_history[-TREND_WINDOW:]
            older = (latency_history[-TREND_WINDOW*2:-TREND_WINDOW]
                     if len(latency_history) >= TREND_WINDOW*2 else recent)
            avg_recent = _median(recent)     # 중앙값 → 단발 burst 거짓 SPIKE 억제
            avg_older = _median(older)
            ratio = avg_recent / avg_older if avg_older > 0 else 1.0

            press_floor.append(avg_recent)
            base_press = min(press_floor) if press_floor else 0.0

            # ★단발 노이즈 억제: 최근 표본 중 SUSTAIN_MIN개 이상이 하한을 넘어야 '지속 상승'으로 인정.
            sustained = sum(1 for v in recent if v > MIN_WARM_PRESSURE) >= SUSTAIN_MIN

            all_nodes = get_live_worker_nodes()
            cap = live_cap or KP_NODE_CAP

            # ★fallback도 '급증(SPIKE)'에만 warm을 띄운다. 완만한 RAMP는 Karpenter가 순차로
            #   따라잡으므로 warm이 비용만 낸다(scaler 경로의 shortfall≥2 원리와 동일).
            abs_hit = avg_recent >= ABS_HIT_PRESSURE and avg_recent >= base_press * BASE_RISE_MULT
            if (abs_hit and sustained) or (ratio >= SPIKE_THRESHOLD and sustained):
                target, reason = min(MAX_WARM_NODES, 2), "SPIKE(급증)"
            elif avg_recent < CALM_ABS or avg_recent <= base_press * CALM_BASE_MULT:
                target, reason = 0, "CALM"
            else:
                # 완만 램프 포함 → warm 추가 없음(현재값 유지, 오르는 중이면 SPIKE에서 잡힘)
                target, reason = current_warm, "HOLD"

            desired, budget = clamp_warm(target, all_nodes, running_pause, current_warm, cap)

            ts = time.strftime('%H:%M:%S')
            info = (f"압박도={avg_recent:.2f} {ratio:.2f}x | 전체노드={all_nodes} "
                    f"stress={get_stress_pods()} warm={current_warm}(run {running_pause}) budget={budget} cap={cap}")
            if desired > current_warm:
                set_replicas(desired)
                cooldown_start = 0
                print(f"  [{ts}] ▲ {reason} → warm {current_warm}→{desired}대 | {info}")
            elif desired < current_warm:
                if cooldown_start == 0:
                    cooldown_start = time.time()
                elif time.time() - cooldown_start >= COOLDOWN_WAIT:
                    set_replicas(desired)
                    cooldown_start = 0
                    print(f"  [{ts}] ▼ {reason} → warm {current_warm}→{desired}대 — 회수 | {info}")
            else:
                cooldown_start = 0

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\n종료 — overprovisioning replicas=0으로 복원")
            set_replicas(0)
            break
        except Exception as e:
            print(f"  [{time.strftime('%H:%M:%S')}] 에러: {e}")
            time.sleep(POLL_INTERVAL)


def _apply(desired, current_warm, reason, all_nodes, stress_nodes, running_pause, budget, cap):
    """scaler 경로용: 목표치 적용 + 관측 로그(총량이 cap에 묶여있음을 눈으로 확인 가능)."""
    ts = time.strftime("%H:%M:%S")
    info = (f"전체노드={all_nodes} stress={stress_nodes} "
            f"warm={current_warm}(run {running_pause}) budget={budget} cap={cap}")
    if desired != current_warm:
        arrow = "▲" if desired > current_warm else "▼"
        if set_replicas(desired):
            print(f"[{ts}] {arrow} warm {current_warm} → {desired}  ({reason}) | {info}")
        else:
            print(f"[{ts}] warm 조절 실패 ({reason}) | {info}")
    else:
        print(f"[{ts}] warm {current_warm} 유지  ({reason}) | {info}")


if __name__ == "__main__":
    main()
