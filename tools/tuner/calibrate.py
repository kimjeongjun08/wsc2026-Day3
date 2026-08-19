#!/usr/bin/env python3
"""calibrate.py — 실측 회차로 모델의 두 부분을 잡는다.

1) 경로 지연 P — 채점 서버 ↔ CloudFront ↔ ALB. 트래픽·노드와 무관한 상수다.
   product 로 잰다(가장 가벼워서 서버측 대기가 거의 없는 앱):
     채점 CSV product p50 15.2ms − 클러스터 내부 product GET p50 1.1ms ≈ 14ms

2) usable / cfs_severity — 큐잉 모델의 두 계수.
   회차별 (트래픽, 노드수, stress 배치) → 앱별 통과율 관측에 격자 탐색으로 맞춘다.
     usable       : 코어 중 실제로 앱 처리에 돌아가는 몫(버스트 흡수 포함)
     cfs_severity : stress 동거 시 CFS 지분 때문에 줄어드는 정도 (0=영향없음, 1=지분대로)

주의: 한때 이 대기를 네트워크 지연으로 보고 E(R)=b+a·R 을 넣었는데 틀렸다.
ALB TargetResponseTime 이 파드 자체가 160~180ms 를 쓰고 있음을 보여줬다.
단발 프로브는 큐를 안 만들어 빠르게 나온다 — 반드시 동시성을 걸어 재라.
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
SLA = {"user": 200, "product": 200, "stress": 1000}

def fit_line(xs, ys):
    """최소제곱 직선 적합. 점이 1개면 원점을 지나는 직선으로 둔다."""
    n = len(xs)
    if n == 0: return 0.0, 0.0
    if n == 1: return (ys[0] / xs[0] if xs[0] else 0.0), 0.0
    mx, my = sum(xs)/n, sum(ys)/n
    den = sum((x-mx)**2 for x in xs)
    a = sum((x-mx)*(y-my) for x, y in zip(xs, ys)) / den if den else 0.0
    return a, my - a*mx

def main():
    obs_f = os.path.join(HERE, "observations.json")
    obs = json.load(open(obs_f)) if os.path.exists(obs_f) else []
    if isinstance(obs, list):                      # 예전 형식 → 새 형식으로 감싼다
        obs = {"rounds": obs, "edge_samples": []}

    # --- 1) 경로 지연 상수 ---
    ps = [x["grader_p50"] - x["incluster_p50"] for x in obs.get("path_samples", [])]
    path = sum(ps) / len(ps) if ps else 14.0
    print(f"경로 지연 P = {path:.1f} ms   (표본 {len(ps)}개)")
    for x, y in zip(obs.get("path_samples", []), ps):
        print(f"   채점 {x['grader_p50']:>6.1f}ms − 내부 {x['incluster_p50']:>5.1f}ms"
              f" = {y:>6.1f}ms   {x.get('note','')}")

    # --- 2) usable 격자 탐색 ---
    import importlib.util
    spec = importlib.util.spec_from_file_location("solve", os.path.join(HERE, "solve.py"))
    solve = importlib.util.module_from_spec(spec); spec.loader.exec_module(solve)

    # 트래픽 키(user_get, product_post ...)에 맞춰 프로파일을 고른다
    keys = set()
    for r in obs["rounds"]: keys.update(r["traffic"])
    profiles = solve.load_profiles(HERE, {k: 0 for k in keys})

    best = None
    for ui in range(10, 101, 2):
      for uu in range(10, 101, 2):
        for cf in range(0, 21, 2):
            cal = {"usable": ui/100, "usable_iso": uu/100,
                   "path_ms": path, "cfs_severity": cf/20}
            sse = 0.0
            for r in obs["rounds"]:
                row = solve.evaluate(profiles, r["traffic"], SLA, 2.0, cal,
                                     int(round(r["nodes"])), r.get("mode", "iso"))
                if not row: continue
                for a, o in r["observed"].items():
                    if a in row["apps"]:
                        sse += (row["apps"][a]["pass_pct"] - min(o, 100.0))**2
            if best is None or sse < best[0]:
                best = (sse, ui/100, uu/100, cf/20)
    sse, usable, usable_iso, cfs = best
    print(f"\n실효 CPU 비율  공유={usable:.2f}  전용={usable_iso:.2f}  "
          f"CFS 심각도={cfs:.2f}   (잔차제곱합 {sse:.0f})")

    cal = {"usable": usable, "usable_iso": usable_iso,
           "path_ms": round(path, 2), "cfs_severity": cfs}
    json.dump(cal, open(os.path.join(HERE, "calibration.json"), "w"), indent=2)

    print(f"\n{'트래픽':>28} {'노드':>4} {'배치':>7}   예측 vs 실측")
    for r in obs["rounds"]:
        row = solve.evaluate(profiles, r["traffic"], SLA, 2.0, cal,
                             int(round(r["nodes"])), r.get("mode", "iso"))
        if not row: continue
        t = ",".join(f"{k}={v:g}" for k, v in r["traffic"].items())
        d = "  ".join(f"{a}: {row['apps'][a]['pass_pct']:.1f} vs {min(o,100.0):.1f}"
                      for a, o in r["observed"].items() if a in row["apps"])
        print(f"{t:>28} {r['nodes']:>4.1f} {r.get('mode','iso'):>7}   {d}")

    json.dump(obs, open(obs_f, "w"), indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
