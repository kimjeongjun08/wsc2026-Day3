#!/usr/bin/env python3
# test-decide.py — 판단 로직을 클러스터·AWS 없이 검증한다. 수 초, 무료.
#   실측 회차 한 번이 38분 + 인프라 비용이다. 분기 검증은 여기서 끝낸다.
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import score, decide


def snap(user=(0, 0, {}), product=(0, 0, {}), stress=(0, 0, {}), win=180.0):
    out = {}
    for name, (req, e5, p) in (("user", user), ("product", product), ("stress", stress)):
        out[name] = {"req": req, "e5": e5, "rps": round(req / win, 2),
                     "p": {str(k): v for k, v in p.items()}}
    return out


CALM  = {10: .01, 30: .02, 50: .03, 70: .05, 80: .06, 90: .09, 95: .11, 99: .16}  # 추정 99%
GOOD  = {10: .02, 30: .03, 50: .05, 70: .08, 80: .10, 90: .14, 95: .18, 99: .30}  # 추정 96%
MID   = {10: .03, 30: .05, 50: .07, 70: .10, 80: .13, 90: .17, 95: .22, 99: .55}  # 추정 93%
EDGE  = {10: .05, 30: .09, 50: .13, 70: .17, 80: .19, 90: .24, 95: .33, 99: .60}
BAD   = {10: .09, 30: .15, 50: .22, 70: .38, 80: .52, 90: .78, 95: .86, 99: 1.4}   # 실측 peak2 user
SCALM = {10: .05, 30: .08, 50: .12, 70: .18, 80: .22, 90: .30, 95: .40, 99: .70}  # 추정 99%
SGOOD = {10: .10, 30: .18, 50: .25, 70: .40, 80: .55, 90: .80, 95: .95, 99: 1.5}  # 추정 95%
SBAD  = {10: .9, 30: 2.1, 50: 4.0, 70: 7.5, 80: 9.9, 90: 14.0, 95: 16.0, 99: 22.0}  # 실측 붕괴


def ledger(minutes, avg_nodes, perf, req_per_min=3000):
    led = score.blank_ledger()
    led["minutes"] = minutes
    led["node_minutes"] = avg_nodes * minutes
    for a in score.APPS:
        r = req_per_min * minutes
        led["req"][a] = r
        led["ok"][a] = r * 0.999
        led["under"][a] = r * perf.get(a, 95) / 100.0
    return led


fails = []


def run(name, led, sn, nodes, memory, want, contains=None, probe=None):
    d, why = decide.advise(led, sn, nodes, memory, probe)
    ok = (d == want) and (contains is None or any(contains in w for w in why))
    print(("  [O] " if ok else "  [X] ") + name + f"  → {d:+d}")
    for w in why:
        print("        " + w)
    if not ok:
        print(f"        !! 기대 {want:+d}" + (f" / '{contains}' 포함" if contains else ""))
        fails.append(name)
    return d


print("== 1. 가용성이 최우선 (되돌릴 수 없다)")
run("5xx 가 보이면 무조건 증설", ledger(60, 3, {"user": 95}),
    snap((9000, 60, GOOD), (9000, 0, GOOD), (600, 0, SGOOD)), 3, {}, +1, "가용성")

print("== 2. 비용 게이트(30%)는 누적으로 지킨다")
run("stress 누적 35% → 게이트 방어", ledger(60, 3, {"user": 95, "product": 99, "stress": 35}),
    snap((9000, 0, GOOD), (9000, 0, GOOD), (600, 0, SBAD)), 3, {}, +1, "게이트")
run("누적 48% 면 게이트가 아니다 (대조군 user)",
    ledger(60, 4, {"user": 48, "product": 99, "stress": 72}),
    snap((9000, 0, BAD), (9000, 0, GOOD), (600, 0, SGOOD)), 4, {"escalation_pays": False},
    0, "이미 확인")

print("== 2b. ★실측 사고 재현: 누적이 나빠도 '지금' 멀쩡하면 사지 않는다")
# 2026-08-21 회차: peak2 에서 누적 user 가 31% 로 떨어진 뒤 현재 통과율이 99% 로
# 회복됐는데도 게이트 규칙이 매 주기 발동해 노드를 4->5->7->8 로 밀어올렸다.
# 게다가 거기서 return 하는 바람에 축소까지 막혀 18rps 계곡을 8대로 완주했다.
PGOOD = {"user": {"pass": 100, "p50": .04, "p90": .08, "max": .15, "n": 15},
         "product": {"pass": 100, "p50": .03, "p90": .07, "max": .12, "n": 15},
         "stress": {"cpu_m": 300, "cpu_pct": 15}}
run("누적 31% + 지금 99% → 안 산다", ledger(14, 4, {"user": 31, "product": 90, "stress": 90}),
    snap((9000, 0, CALM), (9000, 0, CALM), (600, 0, SCALM)), 4, {}, -1, "축소", probe=PGOOD)
PBAD2 = {"user": {"pass": 20, "p50": .9, "p90": 2.2, "max": 4.0, "n": 15},
         "product": {"pass": 100, "p50": .04, "p90": .09, "max": .2, "n": 15},
         "stress": {"cpu_m": 1900, "cpu_pct": 95}}
m = {"escalation_pays": False}
run("누적 31% + 지금도 나쁨 → '증설 무효' 판정을 무시하고 산다",
    ledger(14, 4, {"user": 31, "product": 90, "stress": 90}),
    snap((9000, 0, BAD), (9000, 0, CALM), (600, 0, SGOOD)), 4, m, +1, "게이트", probe=PBAD2)

print("== 3. 증설이 효과 없다는 게 드러나면 그만둔다 (대조군의 함정)")
led = ledger(63, 4, {"user": 48})
m = {"last_upsize": {"nodes": 4, "minute": 60, "perf": {"user": 48.0, "product": 99.0, "stress": 72.0}}}
v = decide.review_upsize(led, m, {"user": 49.0, "product": 99.0, "stress": 72.0})
ok = m.get("escalation_pays") is False and v and "밑지는" in v
print(("  [O] " if ok else "  [X] ") + "노드 늘렸는데 tier 가 안 올라감 → 증설 중단")
print("        " + str(v))
if not ok: fails.append("증설 반증")
m2 = {"last_upsize": {"nodes": 3, "minute": 60, "perf": {"user": 48.0, "product": 99.0, "stress": 72.0}}}
v2 = decide.review_upsize(ledger(63, 3, {"user": 70}), m2,
                          {"user": 93.0, "product": 99.0, "stress": 95.0})
ok = m2.get("escalation_pays") is not False and v2 and "남는" in v2
print(("  [O] " if ok else "  [X] ") + "늘렸더니 tier 를 2점어치 넘김 → 계속 허용")
print("        " + str(v2))
if not ok: fails.append("증설 허용")

print("== 4. 목표(90%) 미달이면 늘린다")
run("아직 안 해봤으면 일단 늘려본다", ledger(25, 2, {"user": 95}),
    snap((9000, 0, EDGE), (9000, 0, GOOD), (600, 0, SGOOD)), 2, {}, +1, "목표 미달")
run("상한에서는 안 늘린다", ledger(25, 8, {"user": 95}),
    snap((9000, 0, BAD), (9000, 0, GOOD), (600, 0, SGOOD)), 8, {}, 0, "상한")

print("== 5. 축소 — 여기가 제일 크게 번다")
run("전 앱 여유 → 한 대 반납", ledger(85, 5, {"user": 97}),
    snap((300, 0, CALM), (300, 0, CALM), (30, 0, SCALM)), 5, {}, -1, "축소")
run("바닥 2대 아래로는 안 내린다", ledger(20, 2, {"user": 99}),
    snap((300, 0, CALM), (300, 0, CALM), (30, 0, SCALM)), 2, {}, 0, "유지")
run("목표선(90%)과 여유선(95%) 사이는 그대로 둔다 — 요요 방지",
    ledger(40, 3, {"user": 95}),
    snap((9000, 0, MID), (9000, 0, CALM), (600, 0, SCALM)), 3, {}, 0, "유지")

print("== 6. 실측(probe)이 방아쇠, CloudWatch 는 원장")
PB = {"user": {"pass": 40, "p50": .35, "p90": .82, "max": 1.2, "n": 15},
      "product": {"pass": 100, "p50": .04, "p90": .09, "max": .2, "n": 15},
      "stress": {"cpu_m": 1900, "cpu_pct": 95}}
PG = {"user": {"pass": 100, "p50": .04, "p90": .08, "max": .15, "n": 15},
      "product": {"pass": 100, "p50": .03, "p90": .07, "max": .12, "n": 15},
      "stress": {"cpu_m": 400, "cpu_pct": 20}}
run("CloudWatch 는 아직 좋아 보여도 실측이 나쁘면 대응한다",
    ledger(30, 2, {"user": 99}), snap((9000, 0, CALM), (9000, 0, CALM), (600, 0, SCALM)),
    2, {}, +1, "목표 미달", probe=PB)
run("실측이 멀쩡하면 CloudWatch 가 나빠도 안 산다 (이미 지나간 구간)",
    ledger(30, 3, {"user": 99}), snap((9000, 0, BAD), (9000, 0, CALM), (600, 0, SGOOD)),
    3, {}, 0, None, probe=PG)
PM = {"user": {"pass": 85, "p50": .12, "p90": .21, "max": .4, "n": 15},
      "product": {"pass": 100, "p50": .04, "p90": .09, "max": .2, "n": 15},
      "stress": {"cpu_m": 1500, "cpu_pct": 75}}
run("애매하면(나쁘지도 여유롭지도 않다) 그대로 둔다 — 요요 방지",
    ledger(60, 4, {"user": 97}), snap((300, 0, CALM), (300, 0, CALM), (30, 0, SCALM)),
    4, {}, 0, "유지", probe=PM)
run("둘 다 여유 → 축소",
    ledger(60, 4, {"user": 97}), snap((300, 0, CALM), (300, 0, CALM), (30, 0, SCALM)),
    4, {}, -1, "축소", probe=PG)

print("== 7. 트래픽이 커지면 증설 판정을 다시 연다 (peak1 에서 반증 → peak2)")
m = {"escalation_pays": False, "no_escalate_rps": 60.0}
run("peak2 로 트래픽 2배 → 다시 시험", ledger(60, 3, {"user": 95}),
    snap((9000, 0, BAD), (9000, 0, CALM), (600, 0, SCALM), win=45.0), 3, m, +1, "다시 연다")
m = {"escalation_pays": False, "no_escalate_rps": 400.0}
run("트래픽 그대로면 판정 유지", ledger(60, 3, {"user": 95}),
    snap((9000, 0, BAD), (9000, 0, CALM), (600, 0, SCALM)), 3, m, 0, "이미 확인")

print("== 8. ★회차 길이가 달라도 같은 판단이 나와야 한다")
same = True
for label, mins, rpm in (("15분 회차 m5", 5, 1300), ("120분 회차 m75", 75, 3000),
                         ("120분 회차 m115", 115, 3000)):
    for tag, sn, nodes, mem in (
            ("피크 미달", snap((9000, 0, BAD), (9000, 0, GOOD), (600, 0, SGOOD)), 3, {}),
            ("전 앱 여유", snap((300, 0, CALM), (300, 0, CALM), (30, 0, SCALM)), 4, {}),
            ("5xx", snap((9000, 60, GOOD), (9000, 0, GOOD), (600, 0, SGOOD)), 3, {})):
        d, _ = decide.advise(ledger(mins, 3, {"user": 95}, rpm), sn, nodes, dict(mem))
        key = (tag, d)
        ref = globals().setdefault("_ref_" + tag, d)
        if ref != d:
            print(f"  [X] {tag}: {label} 에서 {d:+d} (다른 회차에서는 {ref:+d})")
            same = False
if same:
    print("  [O] 15분·120분·회차 막판 — 세 경우 모두 판단이 동일하다")
else:
    fails.append("회차 길이 독립성")

print("== 9. 표본이 적은 앱은 판단에서 뺀다")
d, why = decide.advise(ledger(3, 2, {"user": 99}),
                       snap((12, 0, BAD), (12, 0, BAD), (2, 0, SBAD)), 2, {})
ok = d == 0
print(("  [O] " if ok else "  [X] ") + "조용한 구간의 몇 건짜리 표본으로 노드를 사지 않는다"
      + f"  → {d:+d}")
for w in why: print("        " + w)
if not ok: fails.append("표본 가드")

print("== 10. 통과율 추정이 실측과 맞는가")
est = score.perf_from_percentiles(BAD, 0.200)
ok = 40 <= est <= 60
print(("  [O] " if ok else "  [X] ") + f"실측 peak2 user 분포 → 추정 {est:.1f}% (실제 채점 48.56%)")
if not ok: fails.append("통과율 추정")

print()
print(("전부 통과" if not fails else "실패: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
