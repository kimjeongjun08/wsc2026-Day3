#!/usr/bin/env python3
# score.py — 채점표를 그대로 코드로 옮긴 것. 도구의 모든 판단은 여기서 나온 숫자로 한다.
#
# 왜 필요한가:
#   지금까지 도구는 "ALB 평균 지연이 SLA 를 넘으면 노드를 늘린다"로 움직였다.
#   그건 증상 대응이지 점수 대응이 아니다. 실측 대조군에서 그 규칙은
#   피크에 노드를 6대까지 밀어올렸고, 성능은 tier 를 하나도 못 넘긴 채
#   비용만 12 → 8 로 깎였다 (총 30.0/40).
#
# 채점표에서 반드시 알아야 할 두 가지 (score_csv.py 실측 확인):
#   1) cost_ratio = avg_ec2 / 2  이고, 노드 0.5대마다 정확히 1점이다.
#      → 노드 1대 = 2점. 성능 한 앱의 만점이 4점이니, 노드 2대를 더 써서
#        한 앱을 0% → 100% 로 만들어야 겨우 본전이다.
#   2) avg_ec2 는 '분' 평균이고 성능은 '요청' 비율이다.
#      → 트래픽 없는 1분과 피크 1분의 비용이 같다. 계곡에서 노드를 켜두는 건
#        점수를 그냥 버리는 것이다. 반대로 피크의 짧은 증설은 싸다.
import json

AVAIL_TIERS = [(90.0, 0.5), (87.5, 0.5), (85.0, 0.5), (82.5, 0.5),
               (80.0, 0.5), (70.0, 0.5), (50.0, 0.5), (30.0, 0.5)]
PERF_TIERS = AVAIL_TIERS
COST_TIERS = [(t / 100.0, 1.0) for t in range(100, 376, 25)]
BASELINE_EC2 = 2.0
SLA_S = {"user": 0.200, "product": 0.200, "stress": 1.000}
APPS = ("user", "product", "stress")
PERF_GATE = 30.0          # 셋 중 하나라도 이 밑이면 비용 12점이 통째로 0 이 된다


def tier_high(v, tiers):
    return 0.0 if v is None else sum(p for t, p in tiers if v >= t)


def tier_low(v, tiers):
    return 0.0 if (v is None or v < 0.5) else sum(p for t, p in tiers if v <= t)


def cost_points(avg_ec2):
    return tier_low(avg_ec2 / BASELINE_EC2, COST_TIERS)


def perf_points(perf):
    """perf: {app: 0~100}"""
    return sum(tier_high(perf.get(a), PERF_TIERS) for a in APPS)


def total(perf, avail, avg_ec2, abnormal=4.0):
    """지금까지의 누적치로 매긴 40점. 비용 게이트까지 반영한다."""
    gate = min(perf.get(a, 0.0) or 0.0 for a in APPS)
    cost = cost_points(avg_ec2) if gate >= PERF_GATE else 0.0
    return {
        "abnormal": abnormal,
        "availability": sum(tier_high(avail.get(a), AVAIL_TIERS) for a in APPS),
        "performance": perf_points(perf),
        "cost": cost,
        "gated": gate < PERF_GATE,
        "total": abnormal
        + sum(tier_high(avail.get(a), AVAIL_TIERS) for a in APPS)
        + perf_points(perf) + cost,
    }


# ── 지연 분포에서 "SLA 통과율"을 추정한다 ────────────────────────────────
# 채점되는 값은 평균 지연이 아니라 'SLA 안에 들어온 요청의 비율'이다.
# CloudWatch 는 TargetResponseTime 의 백분위를 준다. 그 안에서 SLA 가 놓인
# 위치를 찾으면 통과율이 바로 나온다. 평균으로 대신하면 안 된다 —
# 실측에서 user 평균은 SLA 를 넘었지만 통과율은 48.6% 였다. 대응이 달라진다.
def perf_from_percentiles(pcts, sla_s):
    """pcts: {백분위(0~100): 초}. 반환: 추정 통과율 0~100."""
    pts = sorted((float(k), float(v)) for k, v in pcts.items() if v is not None)
    if not pts:
        return None
    if sla_s < pts[0][1]:
        # 가장 낮은 백분위보다도 SLA 가 작다 = 그 밑으로만 통과. 0 쪽으로 외삽한다.
        p0, v0 = pts[0]
        return max(0.0, p0 * (sla_s / v0)) if v0 > 0 else 0.0
    if sla_s >= pts[-1][1]:
        return pts[-1][0]
    for (pa, va), (pb, vb) in zip(pts, pts[1:]):
        if va <= sla_s < vb:
            f = 0.0 if vb == va else (sla_s - va) / (vb - va)
            return pa + (pb - pa) * f
    return pts[-1][0]


# ── 회차 원장 ────────────────────────────────────────────────────────────
# 채점은 회차 '전체'의 누적으로 매겨진다. 지금 이 순간이 아니다.
# 그래서 도구도 누적을 들고 있어야 "이미 벌어둔 것"과 "남은 것"을 구분할 수 있다.
#   requests/under_sla : 요청 가중 (성능·가용성)
#   node_minutes/minutes : 분 가중 (비용)
def blank_ledger():
    return {"minutes": 0.0, "node_minutes": 0.0,
            "req": {a: 0.0 for a in APPS}, "ok": {a: 0.0 for a in APPS},
            "under": {a: 0.0 for a in APPS}}


def ledger_add(led, dt_min, nodes, req, ok, under):
    led["minutes"] += dt_min
    led["node_minutes"] += nodes * dt_min
    for a in APPS:
        led["req"][a] += req.get(a, 0.0)
        led["ok"][a] += ok.get(a, 0.0)
        led["under"][a] += under.get(a, 0.0)
    return led


def ledger_metrics(led):
    def pct(a, b):
        return (a / b * 100.0) if b > 0 else None
    avg = (led["node_minutes"] / led["minutes"]) if led["minutes"] > 0 else None
    return (
        {a: pct(led["under"][a], led["req"][a]) for a in APPS},
        {a: pct(led["ok"][a], led["req"][a]) for a in APPS},
        avg,
    )


# ── 노드 1대의 값어치 ────────────────────────────────────────────────────
def cost_delta(led, delta_nodes, remain_min, hold_min=None):
    """노드를 delta 만큼 더(덜) 쓰면 비용 점수가 얼마나 변하나.

    ★hold_min 이 왜 필요한가 — 증설과 축소는 대칭이 아니다.
      증설은 피크 한 구간만 버티고 계곡에서 다시 내린다. 그 비용을 회차 끝까지
      물린 것으로 계산하면 어떤 증설도 이득이 안 나와서 도구가 영영 안 늘린다.
      축소는 반대로 회차 끝까지 유지되므로 remain 전체로 계산하는 게 맞다.
      이 비대칭이 곧 전략이다: 증설은 짧게, 축소는 길게."""
    if led["minutes"] <= 0 or remain_min <= 0:
        return 0.0
    span = remain_min if hold_min is None else max(0.0, min(hold_min, remain_min))
    now_avg = led["node_minutes"] / led["minutes"]
    total_min = led["minutes"] + remain_min
    keep = (led["node_minutes"] + now_avg * remain_min) / total_min
    move = (led["node_minutes"] + now_avg * remain_min + delta_nodes * span) / total_min
    return cost_points(move) - cost_points(keep)


def perf_delta(perf_now, perf_after, led, rem_req):
    """남은 구간의 통과율이 perf_now → perf_after 로 바뀌면 성능 점수가 얼마나 변하나.
    rem_req: {app: 남은 구간에서 들어올 요청 수 추정치}.
    ★비중을 시간으로 나누면 안 된다. 성능은 요청 가중이고 트래픽은 10배씩 오르내린다.
      회차 막판 5분은 시간으로는 4% 지만 baseline 이면 요청으로는 1% 도 안 된다.
      그래서 '지금 rps 가 남은 시간 동안 유지된다'로 추정한다 — 그게 제일 정직하다."""
    out = 0.0
    for a in APPS:
        r = led["req"][a]
        if r <= 0:
            continue
        rem = max(0.0, rem_req.get(a, 0.0))
        a0 = (led["under"][a] + rem * (perf_now.get(a, 0.0) or 0.0) / 100.0) / (r + rem) * 100.0
        a1 = (led["under"][a] + rem * (perf_after.get(a, 0.0) or 0.0) / 100.0) / (r + rem) * 100.0
        out += tier_high(a1, PERF_TIERS) - tier_high(a0, PERF_TIERS)
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(total(*json.loads(sys.argv[1])), ensure_ascii=False, indent=2))
