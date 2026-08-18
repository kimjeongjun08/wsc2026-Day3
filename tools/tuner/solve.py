#!/usr/bin/env python3
"""solve.py — 실측 프로파일 + 트래픽으로 '최고 점수 인프라'를 계산한다.

모델 (전부 실측에서 나옴, 앱/트래픽 바뀌면 값만 갈린다):

  요청 지연  ≈  F  +  d / (1 - ρ)
      F : 고정 오버헤드 (DB·네트워크·직렬화)        ← profile.sh 가 LOAD_MULT≈0 으로 측정
      d : 그 요청의 CPU 작업량                      ← profile.sh 가 기본값에서 측정 (분포 전체)
      ρ : CPU 이용률 = (Σ R_app · E[d_app]) / C     ← 프로세서 셰어링 근사(M/G/1-PS)
      C : 노드수 × vCPU × 사용가능비율

  점수 = 성능티어(각 앱의 SLA 통과율) + 비용티어(노드수) + 고정(비정상4 + 가용12)

  성능 통과율은 가정하지 않고 **실측 d 표본을 그대로 통과**시켜 계산한다 —
  버스트(10% 가 4~8배) 같은 분포의 꼬리가 자동으로 반영된다.

노드수를 1 늘리면 ρ 가 내려가 성능이 오르고 비용이 1~2점 깎인다.
그 교환의 최적점을 전수 탐색으로 찾는다.
"""
import json, argparse, math, os

# 채점 공식 — 채점 서버 score_csv.py 와 동일. 실측 4개 회차로 검증했다.
PERF_TIERS = [90, 87.5, 85, 82.5, 80, 70, 50, 30]        # 각 0.5점, 앱당 최대 4점
COST_TIERS = [1.00 + 0.25*i for i in range(12)]          # 각 1.0점, 최대 12점
BASELINE_EC2 = 2

def perf_points(pct):
    return sum(0.5 for t in PERF_TIERS if pct >= t)

def cost_points(ratio):
    return sum(1.0 for t in COST_TIERS if t >= ratio)

def pass_rate(samples, fixed, rho, sla):
    """실측 CPU 표본을 이용률 rho 로 늘려 SLA 통과율을 낸다."""
    if rho >= 0.99:
        return 0.0
    infl = 1.0 / (1.0 - rho)
    ok = sum(1 for d in samples if fixed + d * infl <= sla)
    return 100.0 * ok / len(samples)

def solve(profiles, traffic, sla, vcpu_per_node, usable, node_min, node_max,
          reserved_nodes=0, usable_iso=None):
    """reserved_nodes: 워크로드와 CPU 를 공유하지 않는 노드(예: stress 전용) 수."""
    if usable_iso is None: usable_iso = usable
    rows = []
    for n in range(node_min, node_max + 1):
        shared = max(1, n - reserved_nodes)
        C = shared * vcpu_per_node * usable            # 밀리코어 환산 없이 '코어'
        # 공유 노드에 올라가는 앱들의 총 CPU 수요(코어)
        demand = sum(traffic[a] * profiles[a]["cpu_ms_mean"] / 1000.0
                     for a in profiles if not profiles[a].get("isolated"))
        rho = demand / C if C > 0 else 1.0
        perf = 0.0
        detail = {}
        for a, p in profiles.items():
            if p.get("isolated"):
                # 전용 노드는 자기들끼리만 경쟁한다
                c_iso = max(1, reserved_nodes) * vcpu_per_node * usable_iso
                r = traffic[a] * p["cpu_ms_mean"] / 1000.0 / c_iso
            else:
                r = rho
            pr = pass_rate(p["cpu_ms_samples"], p["fixed_ms"], r, sla[a])
            detail[a] = {"rho": round(r, 3), "pass_pct": round(pr, 1),
                         "points": perf_points(pr)}
            perf += perf_points(pr)
        ratio = n / BASELINE_EC2
        cost = cost_points(ratio)
        total = 4.0 + 12.0 + perf + cost          # 비정상 4 + 가용성 12 는 이미 만점 유지 중
        rows.append({"nodes": n, "rho": round(rho, 3), "perf": round(perf, 1),
                     "cost": round(cost, 1), "total": round(total, 1), "apps": detail})
    rows.sort(key=lambda r: (-r["total"], r["nodes"]))
    return rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile-dir", default=".")
    ap.add_argument("--traffic", required=True,
                    help='앱별 초당 요청수 JSON. 예: {"user":44,"product":48,"stress":2.5}')
    ap.add_argument("--sla", default='{"user":200,"product":200,"stress":1000}')
    ap.add_argument("--isolated", default='["stress"]', help="전용 노드에 격리된 앱")
    ap.add_argument("--vcpu", type=float, default=2.0)
    ap.add_argument("--usable", type=float, default=None,
                    help="공유 풀 실효 CPU 비율. 생략하면 calibration.json 을 쓴다")
    ap.add_argument("--usable-iso", type=float, default=None)
    ap.add_argument("--reserved-nodes", type=int, default=1)
    ap.add_argument("--min-nodes", type=int, default=2)
    ap.add_argument("--max-nodes", type=int, default=12)
    ap.add_argument("--top", type=int, default=8)
    a = ap.parse_args()

    cal_f = os.path.join(a.profile_dir, "calibration.json")
    cal = json.load(open(cal_f)) if os.path.exists(cal_f) else {}
    usable     = a.usable     if a.usable     is not None else cal.get("usable", 0.31)
    usable_iso = a.usable_iso if a.usable_iso is not None else cal.get("usable_iso", usable)
    print(f"[보정] 공유 usable={usable:.3f} 전용 usable={usable_iso:.3f}"
          + ("" if cal else "  (calibration.json 없음 — 기본값)"))
    traffic = json.loads(a.traffic); sla = json.loads(a.sla)
    isolated = set(json.loads(a.isolated))
    profiles = {}
    for app in traffic:
        f = os.path.join(a.profile_dir, f"profile-{app}.json")
        if not os.path.exists(f):
            print(f"[skip] {f} 없음 — profile.sh {app} 를 먼저 돌려라"); continue
        p = json.load(open(f)); p["isolated"] = app in isolated
        profiles[app] = p
    if not profiles:
        raise SystemExit("프로파일이 없다")

    rows = solve(profiles, traffic, sla, a.vcpu, usable,
                 a.min_nodes, a.max_nodes, a.reserved_nodes, usable_iso)
    print(f"{'노드':>4} {'ρ':>6} {'성능':>5} {'비용':>5} {'합계':>6}   앱별 통과율")
    for r in rows[:a.top]:
        d = "  ".join(f"{k}={v['pass_pct']}%({v['points']})" for k, v in r["apps"].items())
        print(f"{r['nodes']:>4} {r['rho']:>6.2f} {r['perf']:>5.1f} {r['cost']:>5.1f} {r['total']:>6.1f}   {d}")
    best = rows[0]
    print(f"\n최적: 노드 {best['nodes']}대 → 예상 {best['total']}/40")
    json.dump(rows, open(os.path.join(a.profile_dir, "solution.json"), "w"),
              indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
