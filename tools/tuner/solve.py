#!/usr/bin/env python3
"""solve.py — 실측 프로파일 + 트래픽으로 '최고 점수 인프라'를 계산한다.

모델 (전부 실측에서 나옴. 앱이 바뀌면 프로파일만, 채점 환경이 바뀌면 보정만 다시 잡는다):

  채점이 보는 지연  ≈  P  +  F  +  d / (1 - ρ)

    P    : 경로 지연(채점 서버 ↔ CloudFront ↔ ALB). 실측 상수 ~14ms.
           product 로 잰다: 채점 p50 15.2ms − 클러스터 내부 p50 1.1ms.
    F    : 앱 고정 오버헤드 (DB·직렬화)                  ← profile.sh 가 측정
    d    : 요청당 CPU 작업량 분포                        ← profile.sh 가 커널 카운터로 측정
    ρ    : 그 앱이 쓸 수 있는 CPU 대비 수요 = 수요 / (쓸 수 있는 코어 × usable)

  ★지연의 대부분은 '줄 서는 시간'이다. 요청 자체는 CPU 13.6ms 인데, 동시에 몰리면
    코어 수로 나눠 처리하느라 대기가 붙는다. 실측:
        동시성  1 → user POST  20.1ms
        동시성 20 → user POST 104.2ms      (20×13.6ms ÷ 2코어 = 136ms 와 일치)
    그래서 성능을 좌우하는 것은 '노드 수'가 아니라 'user/product 가 쓸 수 있는 코어 수'다.

  ※ 한때 이 대기를 네트워크(엣지) 지연으로 잘못 해석했다. ALB TargetResponseTime 이
    반박한다 — 파드 자체가 160~180ms 를 쓰고 있었다. 단발 프로브는 큐를 안 만들어
    빠르게 나오므로 대표성이 없다. 반드시 동시성을 걸어 재라.

  점수 = 성능티어(앱별 SLA 통과율) + 비용티어(노드수) + 비정상4 + 가용성12

비용은 노드 0.5대당 1점, 성능은 티어당 0.5점 — 비용이 두 배 가파르다.
따라서 최적해는 "성능이 허용되는 선에서 노드를 최대한 줄인 구성"이다.

비용 지표는 계정의 running EC2 '전체 수'다(채점 collector.py 확인). EKS 노드만이
아니라 bastion 같은 부속 인스턴스도 세므로, 노드 수 = 총 인스턴스 수로 다룬다.

stress 배치도 함께 고른다:
  iso    — stress 를 taint 된 전용 노드에 격리. user/product 는 나머지 노드를 독점하지만
           노드 1대를 통째로 쓴다(비용 2점).
  shared — 같은 노드에 태워 1대를 아낀다. 대신 CFS 가 requests 비율로 CPU 를 나누므로
           (stress 600m : user 70m = 8.6배) user 몫이 줄어든다.
           실측 x0.5: 같은 4코어인데 격리 96.99% vs 동거 86.88%.
어느 쪽이 유리한지는 트래픽에 갈리므로 둘 다 계산해서 점수로 비교한다.
"""
import json, argparse, os

# 채점 공식 — 채점 서버 score_csv.py 와 동일.
PERF_TIERS = [90, 87.5, 85, 82.5, 80, 70, 50, 30]        # 각 0.5점, 앱당 최대 4점
COST_TIERS = [1.00 + 0.25*i for i in range(12)]          # 각 1.0점, 최대 12점
BASELINE_EC2 = 2
MIN_SHARED_NODES = 2

def perf_points(pct):
    return sum(0.5 for t in PERF_TIERS if pct >= t)

def cost_points(ratio):
    return sum(1.0 for t in COST_TIERS if t >= ratio)

def pass_rate(samples, fixed, edge, rho, sla):
    """실측 CPU 표본을 이용률 rho 로 늘리고 엣지 지연을 더해 SLA 통과율을 낸다."""
    if rho >= 0.99:
        return 0.0
    infl = 1.0 / (1.0 - rho)
    ok = sum(1 for d in samples if edge + fixed + d * infl <= sla)
    return 100.0 * ok / len(samples)

def load_curves(profile_dir):
    """concurrency.sh 가 만든 동시성-지연 곡선들을 읽는다. {'user-post': [...], ...}"""
    import glob, os, json
    out = {}
    for f in glob.glob(os.path.join(profile_dir, "concurrency-*.json")):
        d = json.load(open(f))
        out[f"{d['app']}-{d['verb']}"] = d["points"]
    return out

def curve_latency(points, c):
    """동시성 c 에서의 p50 지연을 선형보간으로 읽는다."""
    xs = [p["concurrency"] for p in points]
    ys = [p["p50_ms"] for p in points]
    if c <= xs[0]:
        return ys[0]
    for i in range(1, len(xs)):
        if c <= xs[i]:
            t = (c - xs[i-1]) / (xs[i] - xs[i-1])
            return ys[i-1] + t * (ys[i] - ys[i-1])
    return ys[-1] * c / xs[-1]          # 포화 이후는 선형 외삽

def inflation(curves, key, c, scale=1.0):
    """동시성 c 에서 지연이 '한가할 때' 대비 몇 배가 되는가.
    측정된 요청별 CPU 분포에 이 배수를 곱해 SLA 통과율을 계산한다.

    scale: 곡선 자기보정 계수. 곡선은 내가 보낸 요청 본문으로 잰 것이라,
      실제 트래픽의 본문이 다르면 비용이 다르다. 특히 stress 는 요청의 length 에 따라
      비용이 초선형으로 뛴다 (실측: length 88 → 226ms, 150 → 489ms, 250 → 3299ms).
      실전에서는 주입기가 보내는 본문을 볼 수 없으므로, ALB TargetResponseTime 실측과
      곡선 예측의 비율을 scale 로 넣어 숨은 변수(본문·앱버전·DB크기)를 통째로 흡수한다."""
    pts = curves.get(key)
    if not pts or pts[0]["p50_ms"] <= 0:
        return 1.0
    return max(1.0, scale * curve_latency(pts, c) / pts[0]["p50_ms"])

def path_ms(cal):
    """채점 서버 ↔ CloudFront ↔ ALB 왕복. 트래픽·노드와 무관한 상수."""
    return cal.get("path_ms", 14.0)

def load_profiles(profile_dir, traffic):
    """트래픽 키마다 프로파일을 찾는다.

    키는 'user' 처럼 앱만 쓰거나 'user_get' 처럼 메서드까지 쓸 수 있다.
    GET 은 INSERT 가 없어 고정 오버헤드가 절반이라(user: POST 14.8ms vs GET 7.6ms)
    트래픽의 대부분이 GET 인 앱을 POST 로만 모델링하면 통과율을 과소평가한다.
    """
    import os, json
    out = {}
    for key in traffic:
        app, _, verb = key.partition("_")
        cand = [f"profile-{app}-{verb}.json"] if verb else []
        cand.append(f"profile-{app}.json")
        for c in cand:
            f = os.path.join(profile_dir, c)
            if os.path.exists(f):
                out[key] = json.load(open(f)); break
        else:
            print(f"[skip] {key} 프로파일 없음 — profile.sh {app} {verb or 'post'} 를 먼저 돌려라")
    return out

def app_of(key):
    return key.partition("_")[0]

def evaluate(profiles, traffic, sla, vcpu, cal, nodes, mode, curves=None):
    """노드 nodes 대 + stress 배치 mode 구성의 예상 점수.

    지연의 대부분은 '줄 서는 시간'이고, 그걸 만드는 것은 순간 동시성이다.
    채점 주입기는 초 단위로 요청을 몰아서 쏘므로, 한 순간에 도착하는 요청 수는
        c = β × (초당 총요청) / (코어수 / vCPU당유닛)
    이고, 이 c 에서의 지연 배수를 concurrency.sh 가 실측한 곡선에서 읽는다.
    β(몰림 계수)만 회차로 보정하며, 나머지는 전부 실측값이다."""
    usable     = cal.get("usable", 0.4)
    usable_iso = cal.get("usable_iso", usable)
    E = path_ms(cal)

    # mode: shared | iso | iso2 ...  (iso 뒤 숫자는 stress 전용 노드 수)
    #   stress 는 요청 하나가 235ms 라 파드 1개(2코어)가 4~5rps 에서 포화한다.
    #   stress 트래픽이 커지면 전용 노드 1대로는 모자라므로 2대 이상도 후보에 넣는다.
    if mode.startswith("iso"):
        iso_nodes = int(mode[3:]) if len(mode) > 3 else 1
        shared_nodes = nodes - iso_nodes
        iso_apps = {"stress"}
    else:
        shared_nodes, iso_nodes = nodes, 0
        iso_apps = set()
    # ★공유 노드는 최소 2대. 이유 두 가지 —
    #   1) 고가용성(12점): user/product 가 한 노드에만 있으면 그 노드가 죽을 때 전면 중단이다.
    #   2) 실측: 공유 1대(2코어) 구성에서 HPA 가 늘린 파드가 자리를 못 찾아 6개가 Pending 됐다.
    #      솔버는 "코어가 모자란다"는 계산은 하지만 "파드가 아예 못 뜬다"는 모른다.
    if shared_nodes < MIN_SHARED_NODES:
        return None

    shared_demand = sum(traffic[k] * profiles[k]["cpu_ms_mean"] / 1000.0
                        for k in profiles if app_of(k) not in iso_apps)
    # 동거하면 CFS 가 requests 비율로 CPU 를 나눈다 → user/product 가 쓸 수 있는 코어가 준다.
    #   share = Σrequests(user,product) / Σrequests(전체)
    req = cal.get("cpu_requests_m", {"user": 70, "product": 70, "stress": 600})
    if mode == "shared":
        up = req.get("user", 70) + req.get("product", 70)
        share = up / (up + req.get("stress", 600))
        # 완전 포화일 때만 지분대로 갈린다. 실측 보정계수로 그 정도를 조절한다.
        k = cal.get("cfs_severity", 1.0)
        eff = 1.0 - k * (1.0 - share)
    else:
        eff = 1.0
    cores = shared_nodes * vcpu * eff
    rho_shared = shared_demand / (cores * usable)

    # 앱 하나가 GET/POST 여러 키로 쪼개져 있을 수 있다.
    # 채점은 앱 단위로 통과율을 내므로 요청률로 가중평균한다.
    beta = cal.get("burst_beta", 1.0)
    shared_rps = sum(traffic[k] for k in profiles if app_of(k) not in iso_apps)
    iso_rps    = sum(traffic[k] for k in profiles if app_of(k) in iso_apps)
    c_shared = beta * shared_rps / max(1e-9, cores / vcpu)
    c_iso    = beta * iso_rps    / max(1e-9, iso_nodes * vcpu / vcpu) if iso_nodes else 0.0

    agg = {}
    for k, p in profiles.items():
        app = app_of(k)
        c = c_iso if app in iso_apps else c_shared
        if curves:
            # 트래픽 키(user_post, stress)를 곡선 키(user-post, stress-post)로 맞춘다.
            # 메서드를 안 적은 키는 POST 로 본다 — 예전엔 'stress' 가 곡선을 못 찾아
            # 배수 1.0 이 적용됐고, 포화(4~5rps)를 지나도 99.8% 로 예측했다.
            ckey = k.replace("_", "-")
            if ckey not in curves:
                ckey = f"{app}-post" if f"{app}-post" in curves else ckey
            infl = inflation(curves, ckey, c, cal.get("curve_scale", {}).get(app, 1.0))
            ok = sum(1 for d in p["cpu_ms_samples"]
                     if E + p["fixed_ms"] + d * infl <= sla[app])
            pr = 100.0 * ok / len(p["cpu_ms_samples"])
            r = c
        else:
            r = rho_shared
            pr = pass_rate(p["cpu_ms_samples"], p["fixed_ms"], E, r, sla[app])
        w = traffic[k]
        a = agg.setdefault(app, {"rho": r, "num": 0.0, "den": 0.0})
        a["num"] += pr * w; a["den"] += w

    perf, detail = 0.0, {}
    for app, a in agg.items():
        pr = a["num"] / a["den"] if a["den"] else 0.0
        detail[app] = {"rho": round(a["rho"], 3), "pass_pct": round(pr, 1),
                       "points": perf_points(pr)}
        perf += perf_points(pr)

    # ★비용 점수에는 성능 게이트가 있다 (채점 score_csv.py):
    #     cost = tier(...) if min(세 앱 성능) >= 30% else 0
    #   즉 한 앱이라도 무너지면 비용 12점이 통째로 사라진다.
    #   실측: stress 를 7rps 로 올렸더니 동거 구성에서 stress 26% → 비용 0 → 총점 22.5.
    #   "노드를 줄여 비용을 번다"는 전략은 이 선을 넘는 순간 역효과다.
    perf_min = min((d["pass_pct"] for d in detail.values()), default=100.0)
    cost = cost_points(nodes / BASELINE_EC2) if perf_min >= 30.0 else 0.0
    return {"nodes": nodes, "mode": mode, "path_ms": round(E, 1),
            "cores": round(cores, 2), "cost_gated": perf_min < 30.0,
            "rho": round(rho_shared, 3), "perf": round(perf, 1), "cost": round(cost, 1),
            "total": round(4.0 + 12.0 + perf + cost, 1), "apps": detail}

def solve(profiles, traffic, sla, vcpu, cal, node_min, node_max, modes, curves=None):
    rows = [r for n in range(node_min, node_max + 1) for m in modes
            if (r := evaluate(profiles, traffic, sla, vcpu, cal, n, m, curves))]
    rows.sort(key=lambda r: (-r["total"], r["nodes"]))
    return rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile-dir", default=".")
    ap.add_argument("--traffic", required=True,
                    help='앱별 초당 요청수 JSON. 예: {"user":44,"product":48,"stress":2.5}')
    ap.add_argument("--sla", default='{"user":200,"product":200,"stress":1000}')
    ap.add_argument("--vcpu", type=float, default=2.0)
    ap.add_argument("--modes", default="shared,iso,iso2,iso3")
    ap.add_argument("--min-nodes", type=int, default=2)
    ap.add_argument("--max-nodes", type=int, default=12)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--min-shared-nodes", type=int, default=2,
                    help="user/product 가 쓸 공유 노드의 최소 대수 (고가용성·스케줄 여유)")
    a = ap.parse_args()

    global MIN_SHARED_NODES
    MIN_SHARED_NODES = a.min_shared_nodes
    cal_f = os.path.join(a.profile_dir, "calibration.json")
    cal = json.load(open(cal_f)) if os.path.exists(cal_f) else {}
    if not cal:
        print("[경고] calibration.json 이 없다 — calibrate.py 를 먼저 돌려라")
    traffic = json.loads(a.traffic); sla = json.loads(a.sla)
    profiles = load_profiles(a.profile_dir, traffic)
    if not profiles:
        raise SystemExit("프로파일이 없다")

    print(f"[보정] usable={cal.get('usable')} usable_iso={cal.get('usable_iso')} "
          f"경로지연={path_ms(cal):.0f}ms cfs={cal.get('cfs_severity')}")
    curves = load_curves(a.profile_dir)
    print(f"[곡선] {len(curves)}개 로드: {', '.join(sorted(curves))}" if curves
          else "[곡선] 없음 — concurrency.sh 를 먼저 돌려라")
    rows = solve(profiles, traffic, sla, a.vcpu, cal, a.min_nodes, a.max_nodes,
                 a.modes.split(","), curves)
    print(f"{'노드':>4} {'stress':>7} {'코어':>5} {'ρ':>6} {'성능':>5} {'비용':>5} {'합계':>6}   앱별 통과율")
    for r in rows[:a.top]:
        d = "  ".join(f"{k}={v['pass_pct']}%({v['points']})" for k, v in r["apps"].items())
        print(f"{r['nodes']:>4} {r['mode']:>7} {r['cores']:>5.1f} {r['rho']:>6.2f} {r['perf']:>5.1f} "
              f"{r['cost']:>5.1f} {r['total']:>6.1f}   {d}")
    best = rows[0]
    print(f"\n최적: 노드 {best['nodes']}대 / stress={best['mode']} → 예상 {best['total']}/40")
    print(f"적용: ./apply.sh {best['nodes']} {best['mode']}")
    json.dump(rows, open(os.path.join(a.profile_dir, "solution.json"), "w"),
              indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
