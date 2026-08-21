#!/usr/bin/env python3
"""score_pretune.py — pretune 의 자체 부하 결과를 채점 공식으로 환산한다.

표준입력: "<앱> <메서드> <상태코드> <소요초>" 줄들
인자:     <총노드수> <부하시간초>

★표본이 적으면 통과율을 신뢰하지 않는다.
  stress 는 채점 트래픽에서 비율이 3% 뿐이라, 짧은 부하에서는 수십 건밖에 안 들어간다.
  그중 하나만 SLA 를 넘어도 통과율이 0% 로 튀고, 그러면 비용 게이트를 잘못 판정한다
  (실측: stress 0.00% → 24.0점으로 오판). 그래서 최소 표본 수를 요구한다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score as _score          # 채점표는 한 곳에서만 정의한다 (score.py)

SLA = _score.SLA_S
# stress 는 표본이 적으면 판정이 크게 흔들린다.
#   실측: 같은 4노드/iso2 구성인데 84건일 때 42.86%, 239건일 때 79.59% 가 나왔다.
#   앱마다 필요한 최소 표본을 따로 잡는다.
MIN_SAMPLES = {"user": 50, "product": 50, "stress": 150}

def perf_points(p):
    return _score.tier_high(p, _score.PERF_TIERS)

def main():
    nodes = float(sys.argv[1]); dur = float(sys.argv[2])
    ok, tot, posts, n = {}, {}, 0, 0
    for line in sys.stdin:
        p = line.split()
        if len(p) != 4:
            continue
        app, method, code, t = p[0], p[1], p[2], float(p[3])
        if method == "POST":
            posts += 1
        n += 1
        if not code.startswith("2"):
            continue
        tot[app] = tot.get(app, 0) + 1
        if t <= SLA.get(app, 0.2):
            ok[app] = ok.get(app, 0) + 1

    res, thin = {}, []
    for a in ("user", "product", "stress"):
        if tot.get(a, 0) < MIN_SAMPLES[a]:
            thin.append("%s(%d건, %d 필요)" % (a, tot.get(a, 0), MIN_SAMPLES[a]))
            res[a] = None
        else:
            res[a] = 100.0 * ok.get(a, 0) / tot[a]

    solid = {a: v for a, v in res.items() if v is not None}
    perf = sum(perf_points(v) for v in solid.values())
    gate = min(solid.values()) if solid else 0.0
    cost = _score.cost_points(nodes) if gate >= _score.PERF_GATE else 0.0

    fmt = lambda v: "표본부족" if v is None else "%.2f" % v
    print("\t".join([fmt(res["user"]), fmt(res["product"]), fmt(res["stress"]),
                     "%.1f" % perf, "%.1f" % cost, "%.1f" % (4 + 12 + perf + cost),
                     "%.1f" % (n / dur), str(posts)]))
    if thin:
        print("   !! 표본 부족: " + ", ".join(thin)
              + " — DUR 을 늘리거나 W_STRESS 를 올려라", file=sys.stderr)

if __name__ == "__main__":
    main()
