#!/usr/bin/env python3
# decide.py — "지금 노드를 늘릴까 줄일까"를 점수로 판단한다.
#
# 예전 규칙:  ALB 평균 지연 > SLA  →  노드 +1
#   이건 증상 대응이지 점수 대응이 아니다. 실측 대조군(공식 120분)에서 이 규칙은
#   피크에 노드를 6대까지 밀어올렸는데, 성능은 tier 를 하나도 못 넘겼고
#   비용만 12 → 8 로 깎였다. 늘린 노드가 전부 손해였다.
#
# 새 규칙의 뼈대 — 점수표에서 그대로 나온 산수다:
#   · 노드 1대 = 비용 2점 (0.5대마다 1점). 성능은 앱당 최대 4점.
#   · 그래서 "이 노드로 얻을 수 있는 성능 점수의 최대치"가 "비용으로 잃는 점수"보다
#     작으면 그 노드는 무조건 손해다. 모델 없이 확정할 수 있다. 이게 무후회 필터다.
#   · avg_ec2 는 분 평균, 성능은 요청 비율. 트래픽 없는 구간의 노드는 순손실이다.
#     대조군은 하강 구간(min 80~120)에서 평균 4.93대를 켜두고 있었다.
#
# 우선순위:
#   1) 가용성 방어  — 5xx 가 보이면 즉시 증설. 12점짜리이고 되돌릴 수 없다.
#   2) 게이트 방어  — 누적 통과율이 30% 밑으로 가면 비용 12점이 통째로 0 이 된다.
#   3) 목표 추격    — 90% tier 를 못 넘긴 앱이 있으면 늘린다. 단, 이 회차에서
#                    "늘려도 안 움직인다"가 실측으로 드러나면 그때부터 멈춘다.
#   4) 축소         — 전 앱이 여유로우면 즉시 내린다. 여기가 제일 크게 번다.
#
# ★신호를 두 개 쓴다. 역할이 다르다.
#   원장(누적 점수) : CloudWatch 백분위. 채점되는 값 그 자체지만 1~3분 늦다
#                     (실측: 11:47:53 에 최신 데이터포인트가 11:46:00 짜리였다).
#   방아쇠(대응)    : probe.sh 의 실시간 실측. ALB 로 직접 GET 을 쏴서 지금 파드가
#                     어떤지 본다. stress 는 파드 CPU 로 본다(재는 게 곧 부하라서).
#   느린 값으로 방아쇠를 당기면 피크의 20~30% 를 눈감고 지나간다. 성능은 '요청'
#   가중이라 그 구간은 되돌릴 수 없다. 그래서 "지금 나쁜가"는 실측이 정한다.
#
# ★회차 길이를 묻지 않는다.
#   비용은 '분' 평균이다. 그러면 매 분 제약을 만족하는 최소 노드 수를 쓰는 것이
#   회차 길이와 무관하게 평균을 최소로 만든다 — 끝까지 전망할 필요가 없다.
#   예전 버전은 ROUND_MIN 으로 남은 시간을 계산해 "이 증설이 회차 끝까지 얼마"를
#   따졌다. 그러면 15분 회차와 120분 회차가 서로 다른 도구가 된다. 그건 틀렸다.
#   같은 트래픽이면 회차가 길든 짧든 같은 판단이 나와야 한다.
import json, os, sys, time, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score

# ★5xx 문턱은 채점 tier 에 비례해야 한다.
#   가용성 만점 문턱은 90% 다. 오류율 0.67%(가용성 99.33%)면 9%p 나 여유가 있는데
#   예전 문턱 0.3% 는 거기서도 노드를 샀다. 노드 1대는 비용 2점이다.
#   실측(2026-08-21 practice 회차): 시작 1분 만에 300건 중 2건 실패로 증설이 걸렸다.
#   그 곡선의 목표는 분 평균 2.00대라, 한 대만 사도 40점이 날아간다.
#   그래서 두 갈래로 나눈다:
#     E5_SEVERE  진짜 장애다. 지금 당장 늘린다.
#     E5_WARN    잡음일 수 있다. '누적 가용성'이 실제로 깎이고 있을 때만 늘린다.
E5_SEVERE = float(os.environ.get("E5_SEVERE", 5.0))     # 이 위면 즉시
E5_WARN = float(os.environ.get("E5_WARN", 1.0))         # 이 위 + 누적이 나쁘면
AVAIL_DANGER = float(os.environ.get("AVAIL_DANGER", 97.0))
GATE_FAR = float(os.environ.get("GATE_FAR", 20.0))   # 누적이 이보다 낮으면 두 칸씩
GATE_ETA_MIN = float(os.environ.get("GATE_ETA_MIN", 30))  # 이 분 안에 뚫릴 것 같으면 미리 막는다
STEP_RATIO = float(os.environ.get("STEP_RATIO", 2.5))    # 이 배수 이상 뛰면 계단으로 본다
RPS_PER_NODE = float(os.environ.get("RPS_PER_NODE", 40))  # 실측: 8대가 311rps 를 p90 47ms 로 처리
STEP_MIN_RPS = float(os.environ.get("STEP_MIN_RPS", 40))  # 잡음 방지 하한
GATE_DANGER = float(os.environ.get("GATE_DANGER", 40))  # 누적이 이 밑이면 게이트 위험
# ★히스테리시스. 목표 tier 는 90% 다.
#   90 에서 늘리고 90 에서 줄이면 경계에서 노드가 왔다갔다 하고, 그때마다 롤아웃이
#   돌아 가용성이 깎인다. 늘리는 선(90)과 줄이는 선(96)을 벌려 놓는다.
TARGET_PERF   = float(os.environ.get("TARGET_PERF", 90))
# ★축소 문턱은 92 다. 95 가 아니다.
#   백분위는 p99 까지만 읽는다. 그래서 p99 가 SLA 를 조금만 넘으면 추정 통과율이
#   94.5% 근처에서 천장을 친다 — 아무리 한가해도 95 에 영원히 못 닿는다.
#   실측 재생에서 이것 때문에 18rps 계곡에서도 노드가 3대에 붙어 안 내려왔다.
#   채점 tier 는 90 이다. 94.5% 면 이미 만점 구간이고 축소해도 되는 상황이다.
#   90(증설선)과 92(축소선) 사이 간격이 요요를 막는 히스테리시스다.
SCALE_IN_PERF = float(os.environ.get("SCALE_IN_PERF", 92))
# 실측 표본은 15개뿐이라 오차가 ±15%p 쯤 된다. 그래서 판정선을 크게 벌린다.
#   PROBE_BAD  : 이 밑이면 잡음으로 설명이 안 된다 → 지금 나쁘다
#   PROBE_OK   : 이 위여야 축소를 허용한다
PROBE_BAD = float(os.environ.get("PROBE_BAD", 70))
PROBE_OK = float(os.environ.get("PROBE_OK", 93))
STRESS_CPU_BAD = float(os.environ.get("STRESS_CPU_BAD", 88))
STRESS_CPU_OK = float(os.environ.get("STRESS_CPU_OK", 60))
FLOOR = int(os.environ.get("FLOOR_NODES", 2))


# 표본이 이보다 적은 앱은 판단에서 뺀다.
#   1분 해상도로 읽으므로 조용한 구간에는 앱당 수십 건뿐이다. 그중 하나만
#   느려도 통과율이 크게 튀고, 그걸 믿으면 트래픽도 없는데 노드를 산다.
#   pretune 에서 같은 함정을 이미 겪었다(stress 84건일 때 42.86% / 239건일 때 79.59%).
MIN_SAMPLES = float(os.environ.get("MIN_SAMPLES", 30))


def estimate(snap):
    """ALB 스냅샷 → 앱별 {통과율, 성공률, rps}"""
    perf, avail, rps = {}, {}, {}
    for app in score.APPS:
        d = snap.get(app)
        if not d:
            continue
        req, e5 = d.get("req", 0.0), d.get("e5", 0.0)
        if req < MIN_SAMPLES:
            rps[app] = d.get("rps", 0.0)
            perf[app] = None
            avail[app] = None
            continue
        rps[app] = d.get("rps", 0.0)
        avail[app] = (100.0 * (req - e5) / req) if req > 0 else None
        p = score.perf_from_percentiles({int(k): v for k, v in (d.get("p") or {}).items()},
                                        score.SLA_S[app])
        # 채점의 분모에는 실패 요청도 들어간다. 통과는 2xx 만 센다.
        if p is not None and req > 0:
            p = p * (req - e5) / req
        perf[app] = p
    return perf, avail, rps


def probe_trustworthy(probe):
    """probe 가 '앱이 실제로 처리한 응답'을 보고 있는지 확인한다.

    ★앱의 API 가 바뀌면 probe 는 404 를 받는다. 그런데 404 도 빨리 오므로
      "전부 통과"라고 보고한다 — 방아쇠가 영원히 안 울린다.
      실측(2026-08-21): 경로를 일부러 틀리게 하니 pass 100%, p50 10ms 가 나왔다.
      probe 는 도구의 눈이다. 눈이 엉뚱한 걸 보고 있으면 나머지는 다 무의미하다.

      판별은 POST 로 한다. 조회는 대상이 없으면 정상적으로도 404 지만,
      생성은 경로가 맞으면 2xx 가 돌아온다. 하나도 없으면 경로를 의심한다.
      이때는 실측을 버리고 채점값(CloudWatch)으로 판단한다 — 느려도 맞는 쪽이다.
    """
    u = (probe or {}).get("user") or {}
    if "ok2xx" not in u:
        return True, ""          # 옛 형식이면 그대로 믿는다
    if u.get("ok2xx", 0) > 0:
        return True, ""
    return False, ("실측이 앱의 정상 응답(2xx)을 한 건도 못 받았다 — "
                   "경로가 앱과 안 맞는 것 같다. 채점값으로만 판단한다")


def probe_view(probe):
    """실측 스냅샷 → 앱별 (지금 나쁜가, 지금 여유로운가, 표시문구)."""
    bad, ok, shots = [], [], []
    for app in ("user", "product"):
        d = (probe or {}).get(app)
        if not d or not d.get("n"):
            continue
        pv = d["pass"]
        shots.append("%s=%d%%(p90 %.0fms)" % (app, pv, d["p90"] * 1000))
        if pv < PROBE_BAD:
            bad.append(app)
        if pv >= PROBE_OK:
            ok.append(app)
    d = (probe or {}).get("stress")
    if d and "cpu_pct" in d:
        shots.append("stress=CPU %d%%" % d["cpu_pct"])
        if d["cpu_pct"] >= STRESS_CPU_BAD:
            bad.append("stress")
        if d["cpu_pct"] <= STRESS_CPU_OK:
            ok.append("stress")
    return bad, ok, ", ".join(shots)


# probe 로 직접 재는 앱. stress 는 요청 하나가 코어를 통째로 먹어서 못 쏜다.
PROBED = ("user", "product")


def below_now(perf, live, p_bad, probe, memory=None):
    """지금 목표(90%)를 못 내고 있는 앱. 앱마다 '가장 좋은 신호'로 판정한다.

    ★stress 를 CPU 로만 보면 안 된다.
      실측(2026-08-21 2회차): stress 누적 통과율이 26% 였다. 게이트(30%)를 밑돌아
      비용 12점이 통째로 0 이 됐다. 그런데 전용 노드의 CPU 는 문턱(88%) 아래라
      "지금은 괜찮다"로 읽혔고, 그래서 게이트 방어가 발동하지 않았다.
      CPU 는 포화의 신호지 SLA 준수의 신호가 아니다. 70% 에서도 큐가 쌓이면
      요청은 1초를 넘긴다. 채점되는 값은 통과율이지 CPU 가 아니다.
      그래서 probe 로 못 재는 앱(stress)은 CloudWatch 통과율로 판정한다.
      느리지만(1~3분) stress 는 천천히 움직이는 신호라 그 지연을 감당할 수 있다.
    """
    memory = {} if memory is None else memory
    bad = set()
    if probe:
        bad |= {a for a in p_bad if a in PROBED}
        # ★안전망: 실측이 "괜찮다"는데 채점값이 계속 나쁘면 채점값을 믿는다.
        #   실측은 표본이 15개뿐이고, 어떤 이유로든 실제 트래픽보다 싼 경로를
        #   재고 있을 수 있다. 실측(practice 회차): probe 가 7분 내내 100% 라고 하는
        #   동안 실제 통과율은 70% 였고, 그만큼 증설이 늦어 성능 5점을 잃었다.
        #   CloudWatch 는 1~3분 늦지만 '채점되는 값 그 자체'다.
        #   두 주기 연속으로 어긋나면 늦더라도 맞는 쪽을 따른다.
        for a in PROBED:
            if perf.get(a) is not None and perf[a] < TARGET_PERF and a not in bad:
                memory["cw_bad"] = memory.get("cw_bad", {})
                memory["cw_bad"][a] = memory["cw_bad"].get(a, 0) + 1
                if memory["cw_bad"][a] >= int(os.environ.get("CW_OVERRIDE", 2)):
                    bad.add(a)
            elif memory.get("cw_bad", {}).get(a):
                memory["cw_bad"][a] = 0
        # probe 로 못 재는 앱은 느려도 채점값(CloudWatch)으로 본다
        bad |= {a for a in live if a not in PROBED and perf[a] < TARGET_PERF}
        # CPU 가 확실히 포화면 그것도 신호로 인정한다 (지연보다 빠르다)
        bad |= {a for a in p_bad if a not in PROBED}
    else:
        bad |= {a for a in live if perf[a] < TARGET_PERF}
    return sorted(bad)


def advise(led, snap, nodes, memory, probe=None):
    """반환: (delta, 이유들). 회차 길이·남은 시간을 쓰지 않는다."""
    perf, avail, rps = estimate(snap)
    trust, blind_msg = probe_trustworthy(probe)
    if not trust:
        probe = None                      # 실측을 아예 안 쓴다
    p_bad, p_ok, p_shot = probe_view(probe)
    why = []
    total_rps = sum(rps.values())
    live = [a for a in score.APPS if perf.get(a) is not None]
    if not live:
        return 0, ["지표가 아직 없다 — 대기"]
    # ★below 는 주기당 한 번만 계산한다.
    #   안에서 '채점값이 계속 나쁜지' 세는 카운터가 돌기 때문에, 여러 번 부르면
    #   한 주기에 여러 칸이 올라가 안전망이 즉시 발동해버린다.
    below = below_now(perf, live, p_bad, probe, memory)
    shot = ", ".join(f"{a}={perf[a]:.0f}%" for a in live)
    if blind_msg:
        why.append("!! " + blind_msg)
    if p_shot:
        shot += " | 실측 " + p_shot

    # ★"증설해도 소용없다"는 판정은 그때의 트래픽에서만 참이다.
    #   공식 회차는 peak1 → 계곡 → peak2 로 강도가 계단식으로 올라간다.
    #   peak1 에서 반증한 걸 peak2 까지 끌고 가면 정작 필요할 때 못 늘린다.
    #   트래픽이 판정 당시보다 확실히 커졌으면 판정을 무효로 하고 다시 시험한다.
    if memory.get("escalation_pays") is False:
        base = memory.get("no_escalate_rps", 0.0)
        if base > 0 and total_rps > base * float(os.environ.get("RETRY_RPS_RATIO", 1.5)):
            memory.pop("escalation_pays", None)
            why.append(f"트래픽이 {base:.0f} → {total_rps:.0f}rps 로 올랐다 "
                       f"— 증설 판정을 다시 연다")

    # ── 0) 계단 감지 (앞먹임) ────────────────────────────────────────────
    #   ★지표가 나빠지길 기다리면 늦는다.
    #     실측(2026-08-21 공식 120분): 계곡 8rps 에서 peak2 311rps 로 34배 뛰었는데,
    #     도구는 통과율이 떨어지는 걸 확인한 뒤 한 칸씩 올렸다.
    #       m55 계단 → m58 첫 증설(2→3) → m66 5대.  그 사이 4분간 통과율 0%,
    #       분당 7,800건 × 4분 = 31,000건이 오염됐고 누적이 45% → 28% 로 무너져
    #       비용 게이트가 뚫렸다(11점 상실).
    #     계곡 20분을 2대로 버텨 아낀 건 비용 0~1점이다. 1점 아끼고 11점을 잃었다.
    #
    #   해법은 "계곡에도 켜두기"가 아니다(계곡이 길면 그게 더 손해다).
    #   트래픽은 즉시 관측된다 — 나빠지기를 기다릴 이유가 없다.
    #   지금 rps 를 지금 노드로 감당 못 하는 게 뻔하면 바로 그만큼 뛴다.
    memory.pop("step_jump", None)   # ★매 주기 초기화 — 지난 계단이 남으면 안 된다
    # ★노드당 처리량을 실측으로 학습한다.
    #   대회에서는 앱 바이너리도 트래픽 곡선도 미리 알 수 없다. 요청당 CPU 가
    #   이번 연습 앱과 다르면 고정 상수는 그대로 오답이 된다.
    #   "지금 전 앱이 SLA 를 지키고 있다" = "이 노드 수로 이 rps 를 감당한다" 이므로,
    #   그 순간의 rps/노드 를 관측값으로 모은다. 관측 중 가장 큰 값이 그 앱의 능력이다.
    #   한 번도 못 쟀으면 RPS_PER_NODE 초기 추정치를 쓴다.
    _live_ok = [a for a in score.APPS if perf.get(a) is not None]
    if _live_ok and nodes > 0 and total_rps > 0 and all(perf[a] >= TARGET_PERF for a in _live_ok):
        _obs = total_rps / nodes
        if _obs > float(memory.get("cap_rps_per_node") or 0):
            memory["cap_rps_per_node"] = round(_obs, 2)
    seen = memory.get("rps_seen") or {}
    prev_rps = memory.get("last_rps", 0.0)
    memory["last_rps"] = total_rps
    if prev_rps > 0 and total_rps > prev_rps * STEP_RATIO and total_rps > STEP_MIN_RPS:
        # 과거에 '이 정도 rps 를 몇 대로 감당했나'를 기억해 두고 그 값을 쓴다.
        # 기억이 없으면 rps 비례로 잡는다 — 지금 노드로 prev_rps 를 감당했으니
        # total_rps 는 그 비율만큼 필요하다고 본다.
        want = 0
        for r, n in sorted(seen.items(), key=lambda x: -float(x[0])):
            if float(r) >= total_rps * 0.8:
                want = max(want, int(n))
        if not want:
            # ★비례로 잡으면 안 된다. 바닥 2대는 9rps 를 '여유롭게' 처리하던 값이라
            #   34배 뛰었다고 34배가 필요한 게 아니다(실측: 재생에서 8대까지 튀었다).
            #   앞먹임의 값어치는 정확도가 아니라 속도다 — 몇 대가 맞는지는
            #   다음 주기들이 실측으로 다듬는다. 여기서는 계단 크기만큼만 크게 뛴다.
            ratio = total_rps / max(prev_rps, 1.0)
            jump = 1 if ratio < 5 else (2 if ratio < 15 else 3)
            want = nodes + jump
        # ★용량으로도 한 번 잡아본다. 둘 중 큰 쪽을 쓴다.
        #   기억(seen)이 없거나 계단 배수만으로 잡으면 몇 주기를 더 써야 한다.
        #   실측(2026-08-24 D회차): 311rps 를 8대(공유 5 + stress 전용 3)가
        #   p90 47ms 로 처리했고, 6대에서는 p50 407ms 로 SLA 를 못 지켰다.
        #   → 이 부하 구성에서 노드당 약 40rps 가 한계선이다.
        # 노드당 처리량은 앱이 정한다 — 대회에서 어떤 바이너리가 나올지 모르므로
        # 상수를 믿지 않고 이번 회차에서 직접 잰 값을 쓴다.
        # RPS_PER_NODE 는 아직 한 번도 못 재봤을 때의 초기 추정치일 뿐이다.
        # ★학습값은 추정을 '올리는' 방향으로만 쓴다.
        #   한가한 구간에서 잰 rps/노드 는 그 노드의 능력이 아니라 그때 트래픽이
        #   적었을 뿐이다. 실측(2026-08-24 F회차): baseline 에서 3.61rps/노드 로
        #   학습됐고, 그 값으로 계산하면 311rps 에 86대가 필요하다고 나온다.
        #   낮게 잡힌 학습값을 그대로 믿으면 가벼운 앱에서 노드를 왕창 사게 된다.
        est = max(float(memory.get("cap_rps_per_node") or 0), RPS_PER_NODE)
        need = -int(-total_rps // max(est, 1.0))   # 올림
        want = max(want, need)
        want = min(want, int(os.environ.get("MAX_NODES", 8)))
        if want > nodes:
            why.append(f"트래픽 계단 {prev_rps:.0f} → {total_rps:.0f}rps ({total_rps/prev_rps:.0f}배) "
                       f"— 지표를 기다리지 않고 {nodes}→{want}대")
            # ★계단으로 판단한 증설은 바깥에서 깎으면 안 된다.
            #   실측(2026-08-24 D회차): 계단이 +3 을 요청했는데 한 주기 상한 2대에
            #   걸려 2→4 로만 갔고, 8대까지 세 주기가 걸렸다. 그 사이 6분 30초 동안
            #   user p50 이 1.5~5초였고 그 요청들이 전부 SLA 미달로 누적됐다.
            #   앞먹임의 값어치는 속도다. 계단이면 요청한 만큼 한 번에 간다.
            memory["step_jump"] = 1
            return want - nodes, why
    # ★내려가는 계단도 즉시 반영한다.
    #   올릴 때만 빠르고 내릴 때 한 칸씩이면, 피크가 끝난 뒤 한참을 비싸게 쓴다.
    #   비용은 분 평균이라 그 지연이 그대로 점수다.
    #   다만 내리는 건 '지금 여유롭다'가 확인될 때만 한다 — 성급하면 다시 무너진다.
    if (prev_rps > total_rps * STEP_RATIO and nodes > FLOOR
            and total_rps < prev_rps and live
            and all((perf.get(a) or 0) >= SCALE_IN_PERF for a in live)):
        # ★기록을 아무거나 집으면 안 된다.
        #   내려갈 때는 '새 rps 에 가까운' 기록만 의미가 있다. 대역을 제한하지 않으면
        #   피크 때 기록(300rps→8대)을 집어와 결국 안 내려간다(실측: 재생에서 발견).
        want = 0
        for r, n in sorted(seen.items(), key=lambda x: float(x[0])):
            if total_rps <= float(r) <= total_rps * 2:
                want = int(n)
                break
        if not want:
            want = int(round(nodes * total_rps / max(prev_rps, 1.0)))
        want = max(FLOOR, want)
        if want < nodes:
            why.append(f"트래픽 계단 하강 {prev_rps:.0f} → {total_rps:.0f}rps "
                       f"— {nodes}→{want}대로 즉시 반납")
            return want - nodes, why

    # 잘 버티고 있는 조합은 기억해 둔다(다음 계단에서 바로 쓴다)
    if total_rps > 1 and all((perf.get(a) or 0) >= TARGET_PERF for a in live):
        key = str(int(total_rps // 25 * 25))
        if int(seen.get(key, 99)) > nodes:
            seen[key] = nodes
            memory["rps_seen"] = seen

    # ── 1) 가용성 방어 ────────────────────────────────────────────────────
    #   5xx 는 되돌릴 수 없다. 이미 흘린 요청은 회차 끝까지 분모에 남는다.
    def e5rate(a):
        d = snap.get(a, {})
        r = d.get("req", 0)
        return (100.0 * d.get("e5", 0) / r) if r > 30 else 0.0

    cum_perf, cum_avail, _ = score.ledger_metrics(led)
    severe = [a for a in score.APPS if e5rate(a) > E5_SEVERE]
    warn = [a for a in score.APPS
            if e5rate(a) > E5_WARN
            and (cum_avail.get(a) if cum_avail.get(a) is not None else 100.0) < AVAIL_DANGER]
    bad5 = severe or warn
    if bad5:
        why.append("5xx " + ", ".join(f"{a}={e5rate(a):.1f}%" for a in bad5)
                   + (" — 장애 수준, 즉시 증설" if severe
                      else f" + 누적 가용성 저하 — 증설"))
        return +1, why

    # ── 2) 비용 게이트 방어 ───────────────────────────────────────────────
    #   누적 통과율이 30% 밑이면 비용 12점이 통째로 0 이 된다. 그건 막아야 한다.
    #
    #   ★그런데 '누적'만 보고 노드를 사면 절대 안 된다.
    #     누적은 과거다. 지금 이미 잘 하고 있으면 노드를 더 산다고 누적이
    #     더 빨리 오르지 않는다 — 이미 낼 수 있는 최선을 내고 있는 것이다.
    #     실측 사고(2026-08-21 회차): peak2 에서 누적 user 가 31% 로 떨어진 뒤,
    #     현재 통과율이 99% 로 완전히 회복됐는데도 이 규칙이 매 주기 발동해
    #     노드를 4 → 5 → 7 → 8 대로 밀어올렸다. 게다가 여기서 return 하는 바람에
    #     아래 축소 규칙까지 막혀서, 트래픽이 18rps 로 빠진 계곡 구간을 8대로
    #     완주했다. 비용을 두 번 태운 셈이다.
    #
    #   그래서 조건은 둘 다여야 한다: 누적이 위험하고 + 지금도 못 내고 있고.
    #   지금도 못 내고 있다면 그건 아래 3)이 처리한다. 이 규칙의 유일한 역할은
    #   "이 회차엔 증설이 안 통한다"는 판정을 무시하고서라도 사게 하는 것이다.
    cum, _, _ = score.ledger_metrics(led)
    failing = set(below)

    # ★게이트는 '뚫린 뒤'가 아니라 '뚫리기 전에' 막아야 한다.
    #   누적은 요청 가중이라 피크가 시작되면 되돌릴 수 없는 속도로 떨어진다.
    #   실측(2026-08-21 공식 회차): user 누적이 23:26 에 48.09% 였는데
    #   23:31 에 28.43% 가 됐다. 5분이다. 떨어진 뒤에는 노드를 아무리 넣어도
    #   못 되돌린다 — 이미 흘린 요청이 분모에 영원히 남기 때문이다.
    #   그래서 '지금 속도면 몇 분 뒤 뚫리는가'를 계산해 그 전에 움직인다.
    #
    #   여기서는 노드값을 따지지 않는다. 비용 tier 는 완만해서 노드를 2배로 써도
    #   4점만 잃는데(12→8), 게이트가 뚫리면 12점을 통째로 잃는다.
    #   피크에서 노드를 아끼는 건 4점 벌자고 12점을 거는 도박이다.
    #   실측: 대조군은 평균 3.87대로 헤프게 써서 게이트를 지켜 30.0 을 받았고,
    #         우리는 2.38대로 알뜰하게 쓰다가 게이트를 뚫려 17.5 를 받았다.
    # ★예측에는 두 신호 중 '나쁜 쪽'을 쓴다.
    #   CloudWatch 는 채점값이지만 1~3분 늦고, 실측(probe)은 빠르지만 표본이 적다.
    #   둘 중 하나라도 나쁘면 누적은 이미 끌려 내려가는 중이다.
    #   여기서 낙관하면 되돌릴 수 없는 손실이 된다 — 비관 쪽으로 틀리는 게 맞다.
    eta = {}
    for a in live:
        pv = perf[a]
        pp = (probe or {}).get(a, {}).get("pass")
        if pp is not None:
            pv = min(pv, pp)
        t = score.minutes_to_gate(led, a, pv, rps.get(a, 0.0))
        if t is not None:
            eta[a] = t
    soon = sorted((t, a) for a, t in eta.items() if t <= GATE_ETA_MIN)
    if soon and nodes < int(os.environ.get("MAX_NODES", 8)):
        t0, a0 = soon[0]
        step = 3 if t0 <= GATE_ETA_MIN / 3 else 2
        step = min(step, int(os.environ.get("MAX_NODES", 8)) - nodes)
        why.append(f"게이트 예측: {a0} 누적 {cum.get(a0) or 0:.0f}% 가 지금 속도로 "
                   f"{t0:.0f}분 뒤 30% 밑으로 간다 — 미리 {step}대 증설 [{shot}]")
        memory.pop("escalation_pays", None)
        return step, why

    danger = [a for a in live
              if (cum.get(a) if cum.get(a) is not None else 100.0) < GATE_DANGER
              and a in failing]
    if danger and nodes < int(os.environ.get("MAX_NODES", 8)):
        # ★게이트는 tier 가 아니라 절벽이다. 한 칸씩 오를 이유가 없다.
        #   비용 12점이 통째로 걸려 있고, 넘기 전까지는 한 푼도 못 받는다.
        #   한 주기 1대 규칙은 'tier 를 쫓을 때' 과잉지출을 막으려는 것이고,
        #   절벽 앞에서는 오히려 손해다 — 실측(2026-08-21 ambush): 12분 회차에서
        #   1대씩 올라 6대까지밖에 못 갔고 게이트를 못 넘어 비용 0 으로 끝났다.
        #   평균 3.5대면 게이트만 열려도 비용 9점이다. 2~3점 더 내고 9점을 연다.
        #   격차가 클수록 크게 벌린다(최대 2대).
        gap = min(cum[a] for a in danger)
        step = 2 if (gap < GATE_FAR and nodes + 2 <= int(os.environ.get("MAX_NODES", 8))) else 1
        why.append("누적 통과율 "
                   + ", ".join(f"{a}={cum[a]:.0f}%" for a in danger)
                   + f" 이고 지금도 못 내고 있다 — 비용 게이트(30%) 방어, {step}대 증설 [{shot}]")
        memory.pop("escalation_pays", None)
        return step, why

    # ── 0b) 용량 부족 즉시 해소 ────────────────────────────────────────
    #   계단은 '배수'로 잡으므로 전환 중의 부분값을 보면 필요량을 낮게 잡는다.
    #   실측(2026-08-24 E회차): 계단이 109rps 를 보고 3대만 요청했고, 다음 주기는
    #   169rps 였지만 배수가 1.5라 계단이 아니어서 다시 한 칸씩 올라갔다.
    #   배수와 무관하게, 지금 용량이 모자라고 실제로 밀리고 있으면 필요량까지 간다.
    #   ★반드시 '밀리는 중'일 때만 쓴다. 여유로울 때 쓰면 계곡에서 노드를 사들인다.
    if total_rps > 0 and nodes > 0:
        _est = max(float(memory.get("cap_rps_per_node") or 0), RPS_PER_NODE)
        _need = -int(-total_rps // max(_est, 1.0))
        _need = min(_need, int(os.environ.get("MAX_NODES", 8)))
        _hurting = bool(below) or not all(
            (probe or {}).get(a, {}).get("pass", 100) >= PROBE_OK for a in PROBED)
        # 크게 모자랄 때만 쓴다. 한두 대 차이는 기존 규율대로 한 칸씩 사고
        # 3분 뒤 효과를 채점한다 — 그게 과잉 구매를 막는 장치다.
        if _need >= nodes + 2 and _hurting:
            why.append(f"용량 부족 — {total_rps:.0f}rps 에 노드당 {_est:.0f}rps 면 "
                       f"{_need}대가 필요하다 ({nodes}→{_need}대)")
            memory["step_jump"] = 1
            return _need - nodes, why

    # ── 3) 목표 추격 ──────────────────────────────────────────────────────
    #   90% 를 못 넘긴 앱이 있으면 늘린다. 다만 이 회차에서 증설이 효과 없다는 게
    #   실측으로 드러났으면 그만둔다. 대조군이 정확히 그 함정에 빠졌다 —
    #   2→4→5→6 대로 올리는 동안 user 통과율은 48% 근처에 붙어 있었고
    #   비용만 12 → 8 로 깎였다. 노드 1대는 2점이고 성능은 앱당 최대 4점이다.
    # "지금 나쁜가"는 실측이 정한다(below_now). CloudWatch 가 나빠도 실측이
    # 멀쩡하면 이미 지나간 구간이므로 사지 않는다 — 다만 두 주기 연속이면 따른다.
    if below:
        # ★한 번에 한 대씩, 효과를 보고 나서 다음.
        #   노드는 뜨는 데 2~3분 걸린다. 그 사이에 또 사면 3~4대를 한꺼번에 지르고,
        #   나중에 그게 필요했는지 아닌지도 알 수 없게 된다. 대조군이 그렇게 6대까지 갔다.
        #   직전 증설의 효과가 아직 안 나왔으면 기다린다.
        if memory.get("last_upsize"):
            why.append(f"목표 미달[{shot}] — 직전 증설 효과를 아직 못 봤다, 반영 대기")
            return 0, why
        if memory.get("escalation_pays") is False:
            why.append(f"목표 미달[{shot}] 이지만 이 회차에서 증설이 통과율을 "
                       f"못 움직인다는 걸 이미 확인했다 — 유지 ({nodes}대)")
            return 0, why
        if nodes >= int(os.environ.get("MAX_NODES", 8)):
            why.append(f"목표 미달[{shot}] 이지만 상한 {nodes}대 — 더 못 늘린다")
            return 0, why
        why.append(f"목표 미달[{shot}] → 증설 ({nodes}→{nodes+1}대)")
        return +1, why

    # ── 4) 축소 ───────────────────────────────────────────────────────────
    #   비용은 '분' 평균이다. 트래픽이 없는 1분과 피크 1분의 값이 같다.
    #   실측 대조군은 하강 구간 40분을 평균 4.93대로 버텨 비용 2점을 그냥 버렸다.
    #   전 앱이 여유선(96%) 위면 한 대 반납한다. 모자라면 3)이 다시 올린다.
    #   ★축소는 두 신호가 모두 여유로울 때만 한다. 느린 쪽만 보고 내리면
    #     방금 올라온 부하를 못 보고 용량을 뺏는다.
    calm_slow = all(perf[a] >= SCALE_IN_PERF for a in live)
    # 빠른 신호: probe 로 재는 앱은 실측 통과율이 여유선 위여야 한다.
    fast_apps_ok = all((probe or {}).get(a, {}).get("pass", 100) >= PROBE_OK for a in PROBED)
    # stress 는 CPU 로 본다. 다만 CPU 는 보조 신호다 —
    #   포화가 아니거나(60% 이하), 채점값인 통과율이 이미 여유선 위면 여유로 본다.
    #   CPU 만 보면 stress 가 종일 70% 로 도는 회차에서 영원히 축소를 못 한다.
    sc = (probe or {}).get("stress", {}).get("cpu_pct")
    stress_ok = (sc is None or sc <= STRESS_CPU_OK
                 or (perf.get("stress") is not None and perf["stress"] >= SCALE_IN_PERF))
    calm_fast = (not probe) or (not below and fast_apps_ok and stress_ok)
    if nodes > FLOOR and calm_slow and calm_fast:
        why.append(f"전 앱 여유[{shot}] → 축소 ({nodes}→{nodes-1}대, {total_rps:.0f}rps)")
        return -1, why

    why.append(f"유지 ({nodes}대, {shot}, {total_rps:.0f}rps)")
    return 0, why


def review_upsize(led, memory, perf):
    """직전 증설이 실제로 tier 를 넘겼는지 회차 안에서 채점한다.
    못 넘겼으면 그 다음부터 증설을 끈다 — 같은 실수를 회차 내내 반복하지 않는다.
    ★여기도 회차 길이를 안 쓴다. '노드를 한 대 더 줬는데 tier 가 올라갔나'만 본다."""
    up = memory.get("last_upsize")
    if not up:
        return None
    if led["minutes"] - up["minute"] < float(os.environ.get("UPSIZE_REVIEW_MIN", 3)):
        return None
    memory.pop("last_upsize", None)
    before, after = up["perf"], perf
    gained = sum(score.tier_high(after.get(a), score.PERF_TIERS)
                 - score.tier_high(before.get(a), score.PERF_TIERS)
                 for a in score.APPS if before.get(a) is not None and after.get(a) is not None)
    # 노드 1대는 비용 2점짜리다. tier 를 최소 4칸(2점)은 넘겨야 본전이다.
    if gained < 2.0:
        memory["escalation_pays"] = False
        memory["no_escalate_rps"] = up.get("rps", 0.0)
        return (f"직전 증설({up['nodes']}→{up['nodes']+1}대) 검증: 성능 {gained:+.1f}점. "
                f"노드 1대는 비용 2점이다 — 밑지는 장사라 이 회차에서는 더 안 늘린다")
    return f"직전 증설({up['nodes']}→{up['nodes']+1}대) 검증: 성능 {gained:+.1f}점 — 남는 장사다"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--probe", default=None,
                    help="probe.sh 출력(JSON 문자열 또는 파일). 대응 방아쇠로 쓴다")
    ap.add_argument("--nodes", type=int, required=True)
    ap.add_argument("--ledger", default=".round-ledger.json")
    ap.add_argument("--dt", type=float, default=None,
                    help="경과 분. 안 주면 직전 호출과의 실제 시간차를 쓴다(권장)")
    ap.add_argument("--no-commit", action="store_true")
    a = ap.parse_args()

    snap = json.loads(open(a.snapshot).read() if os.path.exists(a.snapshot) else a.snapshot)
    probe = None
    if a.probe:
        try:
            probe = json.loads(open(a.probe).read() if os.path.exists(a.probe) else a.probe)
        except Exception:
            probe = None
    # ★원장이 깨졌으면 조용히 새로 시작한다.
    #   쓰는 도중에 죽으면 JSON 이 반쯤 남는다. 그걸 못 읽고 예외로 죽으면
    #   운영 루프가 매 주기 실패하고, 도구가 아무 판단도 안 하는 상태가 된다.
    #   증상이 "조용히 아무것도 안 함"이라 알아채기가 제일 어렵다.
    st = {"led": score.blank_ledger(), "memory": {}}
    if os.path.exists(a.ledger):
        try:
            st = json.load(open(a.ledger))
            st["led"]["minutes"]  # 모양 확인
        except Exception as e:
            print(f"   원장이 깨져 있어 새로 시작한다 ({type(e).__name__})")
            st = {"led": score.blank_ledger(), "memory": {}}
    led, memory = st["led"], st.get("memory", {})

    # ★경과 시간은 벽시계로 잰다.
    #   비용은 '분' 평균이라 원장의 시간축이 곧 점수다. 주기 설정값(INTERVAL)을
    #   그대로 쓰면 안 된다 — 과부하 때 주기가 20초로 짧아지고, 재시작·API 지연으로도
    #   흔들린다. 그러면 노드가 오래 켜져 있던 구간이 짧게 기록돼 비용을 과소평가한다.
    now = time.time()
    dt = a.dt
    if dt is None:
        last = st.get("last_ts")
        dt = min(10.0, max(0.0, (now - last) / 60.0)) if last else 0.0
    st_last = now

    perf, avail, rps = estimate(snap)
    # ★표본이 모자라 통과율을 못 낸 앱은 원장에 아예 넣지 않는다.
    #   예전엔 요청 수만 더하고 통과는 0 으로 넣었다. 그러면 조용한 구간마다
    #   그 앱의 누적 통과율이 0 쪽으로 끌려가고, 결국 "게이트(30%) 위험"으로
    #   오판해 필요도 없는 노드를 산다. 모르는 건 세지 않는 게 맞다.
    req, ok, under = {}, {}, {}
    for x in score.APPS:
        if perf.get(x) is None:
            continue
        r = snap.get(x, {}).get("req", 0.0)
        req[x] = r
        ok[x] = r - snap.get(x, {}).get("e5", 0.0)
        under[x] = r * perf[x] / 100.0
    # 트래픽이 시작된 뒤부터만 원장에 쌓는다 — 대기 시간이 비용 평균을 왜곡한다
    if sum(rps.values()) >= 1.0 and dt > 0:
        score.ledger_add(led, dt, a.nodes, req, ok, under)
        memory["started"] = True

    verdict = review_upsize(led, memory, perf)
    delta, why = advise(led, snap, a.nodes, memory, probe)
    if verdict:
        why.insert(0, verdict)
    if delta > 0:
        memory["last_upsize"] = {"nodes": a.nodes, "minute": led["minutes"],
                                 "rps": sum(rps.values()),
                                 "perf": {k: v for k, v in perf.items() if v is not None}}

    cp, ca, avg = score.ledger_metrics(led)
    for line in why:
        print("   " + line)
    if led["minutes"] > 0:
        s = score.total({k: v for k, v in cp.items()}, {k: v for k, v in ca.items()}, avg or 2.0)
        print(f"   누적 {led['minutes']:.0f}분 · 평균 {avg:.2f}대 · 예상 "
              f"{s['total']:.1f}/40 (성능 {s['performance']:.1f} 비용 {s['cost']:.1f})"
              + ("  ★게이트 걸림" if s["gated"] else ""))
    if not a.no_commit:
        # 원자적으로 쓴다 — 쓰는 도중에 죽어도 반쪽 파일이 안 남는다.
        tmp = a.ledger + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"led": led, "memory": memory, "last_ts": st_last}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, a.ledger)
    # 배치 결정에 쓸 근거도 같이 넘긴다.
    # ★판단과 배치가 같은 근거를 봐야 한다.
    #   예전엔 판단은 below_now(실측 우선)로 하고, 배치용 BAD 는 CloudWatch 만 봤다.
    #   그래서 "stress 때문에 늘린다"고 결정해놓고 배치는 stress 를 모른 채
    #   공유 노드를 붙였다(실측 3회차: 3/shared → 4/shared).
    #   stress 는 전용 노드로 빼야 CFS 지분을 확보하는데 그게 영영 안 일어난다.
    _pb, _po, _ = probe_view(probe)
    _live = [x for x in score.APPS if perf.get(x) is not None]
    # advise 가 이미 세었으므로 여기서는 카운터를 건드리지 않는다(사본을 넘긴다)
    bad = below_now(perf, _live, _pb, probe, dict(memory))
    worst = min((x for x in score.APPS if perf.get(x) is not None),
                key=lambda x: perf[x], default="")
    # ★stress 가 CPU 를 얼마나 먹고 있는지도 밖으로 내보낸다.
    #   user/product 만 무너지는 구간에서 공유 노드를 붙이면 그 빈 CPU 를
    #   stress 가 먼저 채운다. 배치를 정하려면 이 값이 필요하다.
    _sc = (probe or {}).get("stress", {}).get("cpu_pct")
    print(f"STRESS_CPU={-1 if _sc is None else int(_sc)}")
    print(f"STEP={int(bool(memory.get('step_jump')))}")
    print(f"CAP_RPS_PER_NODE={memory.get('cap_rps_per_node') or 0}")
    print(f"BAD={','.join(bad)}")
    print(f"WORST={worst}")
    print(f"DELTA={delta}")


if __name__ == "__main__":
    main()
