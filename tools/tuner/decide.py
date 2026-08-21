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

E5_ALERT   = float(os.environ.get("E5_ALERT", 0.3))    # 5xx 비율 % — 이 위면 가용성 위험
GATE_DANGER = float(os.environ.get("GATE_DANGER", 40))  # 누적이 이 밑이면 게이트 위험
# ★히스테리시스. 목표 tier 는 90% 다.
#   90 에서 늘리고 90 에서 줄이면 경계에서 노드가 왔다갔다 하고, 그때마다 롤아웃이
#   돌아 가용성이 깎인다. 늘리는 선(90)과 줄이는 선(96)을 벌려 놓는다.
TARGET_PERF   = float(os.environ.get("TARGET_PERF", 90))
SCALE_IN_PERF = float(os.environ.get("SCALE_IN_PERF", 95))
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


def below_now(perf, live, p_bad, probe):
    """지금 목표(90%)를 못 내고 있는 앱.
    실측이 있으면 실측이 정한다 — CloudWatch 는 몇 분 전 이야기다."""
    if probe:
        return sorted(set(p_bad))
    return [a for a in live if perf[a] < TARGET_PERF]


def advise(led, snap, nodes, memory, probe=None):
    """반환: (delta, 이유들). 회차 길이·남은 시간을 쓰지 않는다."""
    perf, avail, rps = estimate(snap)
    p_bad, p_ok, p_shot = probe_view(probe)
    why = []
    total_rps = sum(rps.values())
    live = [a for a in score.APPS if perf.get(a) is not None]
    if not live:
        return 0, ["지표가 아직 없다 — 대기"]
    shot = ", ".join(f"{a}={perf[a]:.0f}%" for a in live)
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

    # ── 1) 가용성 방어 ────────────────────────────────────────────────────
    #   5xx 는 되돌릴 수 없다. 이미 흘린 요청은 회차 끝까지 분모에 남는다.
    bad5 = [a for a in score.APPS
            if snap.get(a, {}).get("req", 0) > 30
            and 100.0 * snap[a].get("e5", 0) / snap[a]["req"] > E5_ALERT]
    if bad5:
        why.append(f"5xx 발생[{','.join(bad5)}] — 가용성 12점 방어, 즉시 증설")
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
    failing = set(below_now(perf, live, p_bad, probe))
    danger = [a for a in live
              if (cum.get(a) if cum.get(a) is not None else 100.0) < GATE_DANGER
              and a in failing]
    if danger and nodes < int(os.environ.get("MAX_NODES", 8)):
        why.append("누적 통과율 "
                   + ", ".join(f"{a}={cum[a]:.0f}%" for a in danger)
                   + f" 이고 지금도 못 내고 있다 — 비용 게이트(30%) 방어 증설 [{shot}]")
        memory.pop("escalation_pays", None)
        return +1, why

    # ── 3) 목표 추격 ──────────────────────────────────────────────────────
    #   90% 를 못 넘긴 앱이 있으면 늘린다. 다만 이 회차에서 증설이 효과 없다는 게
    #   실측으로 드러났으면 그만둔다. 대조군이 정확히 그 함정에 빠졌다 —
    #   2→4→5→6 대로 올리는 동안 user 통과율은 48% 근처에 붙어 있었고
    #   비용만 12 → 8 로 깎였다. 노드 1대는 2점이고 성능은 앱당 최대 4점이다.
    # "지금 나쁜가"는 실측이 정한다(below_now). CloudWatch 가 나빠도 실측이
    # 멀쩡하면 이미 지나간 구간이므로 사지 않는다.
    below = below_now(perf, live, p_bad, probe)
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
    calm_fast = (not probe) or (not p_bad and len(p_ok) >= len([
        a for a in ("user", "product", "stress") if (probe or {}).get(a)]))
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
    # 배치 결정에 쓸 근거도 같이 넘긴다 — 어느 앱이 밀리는지에 따라
    # 노드를 '공유'로 붙일지 'stress 전용'으로 붙일지가 갈린다.
    bad = [x for x in score.APPS
           if perf.get(x) is not None and perf[x] < (90.0 if x != "stress" else 90.0)]
    worst = min((x for x in score.APPS if perf.get(x) is not None),
                key=lambda x: perf[x], default="")
    print(f"BAD={','.join(bad)}")
    print(f"WORST={worst}")
    print(f"DELTA={delta}")


if __name__ == "__main__":
    main()
