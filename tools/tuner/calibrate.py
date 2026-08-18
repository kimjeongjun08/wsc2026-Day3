#!/usr/bin/env python3
"""calibrate.py — 실제 회차 결과로 모델의 유효용량 계수를 역산한다.

왜 필요한가:
  d/(1-ρ) 는 "모든 요청이 전체 CPU 를 공유한다"는 가정이다. 실제로는 ALB 가
  파드마다 독립된 큐로 요청을 나눠서, 한 파드가 버스트 요청을 처리하는 동안
  노드에 CPU 가 남아도 그 파드의 다음 요청은 기다린다. 그래서 실효 용량이
  이론값보다 작다.

  이 계수(usable)를 실측 회차에서 역산해 두면, 앱이나 트래픽이 바뀌어도
  같은 절차로 다시 맞출 수 있다. 가정을 고치는 대신 현실에 맞춘다.

입력: observations.json  — [{"traffic":{...}, "nodes":3.53, "observed":{"user":74.28,...}}, ...]
출력: 최적 usable 과 각 관측의 예측 오차
"""
import json, sys, os
from solve import pass_rate

HERE = os.path.dirname(os.path.abspath(__file__))

def load_profiles(apps, d=HERE):
    p = {}
    for a in apps:
        f = os.path.join(d, f"profile-{a}.json")
        if os.path.exists(f):
            p[a] = json.load(open(f))
    return p

def predict(profiles, traffic, nodes, usable, usable_iso=None, vcpu=2.0, reserved=1.0,
            isolated=("stress",)):
    """usable      : 공유 풀의 실효 CPU 비율
       usable_iso  : 전용(격리) 풀의 실효 비율. 파드가 적고 노드를 독점해서
                     큐 분산 손실이 작다 — 실측상 공유 풀보다 높다."""
    if usable_iso is None: usable_iso = usable
    shared = max(0.5, nodes - reserved)
    C = shared * vcpu * usable
    demand = sum(traffic.get(a,0) * profiles[a]["cpu_ms_mean"]/1000.0
                 for a in profiles if a not in isolated)
    rho = min(0.995, demand / C) if C > 0 else 0.995
    out = {}
    for a, pr in profiles.items():
        if a in isolated:
            c_iso = max(0.5, reserved) * vcpu * usable_iso
            r = min(0.995, traffic.get(a,0)*pr["cpu_ms_mean"]/1000.0 / c_iso)
        else:
            r = rho
        sla = 1000 if a == "stress" else 200
        out[a] = pass_rate(pr["cpu_ms_samples"], pr["fixed_ms"], r, sla)
    return out, rho

def main():
    obs = json.load(open(sys.argv[1] if len(sys.argv)>1 else os.path.join(HERE,"observations.json")))
    apps = sorted({a for o in obs for a in o["traffic"]})
    profiles = load_profiles(apps)
    best = None
    grid = [0.02 + 0.005*i for i in range(int((1.2-0.02)/0.005)+1)]
    for u in grid:
        for ui in grid[::4]:
            err = 0.0
            for o in obs:
                pred,_ = predict(profiles, o["traffic"], o["nodes"], u, ui)
                for a, v in o["observed"].items():
                    # 관측 통과율이 100 을 넘는 경우가 있다(채점기 분모 차이).
                    # 100 으로 잘라야 "이미 만점인 앱" 이 계수를 왜곡하지 않는다.
                    if a in pred: err += (pred[a]-min(100.0, v))**2
            if best is None or err < best[2]: best = (u, ui, err)
    usable, usable_iso, err = best
    print(f"보정: 공유 usable={usable:.3f}  전용 usable={usable_iso:.3f}  (잔차제곱합 {err:.0f})")
    print(f"{'트래픽':>22} {'노드':>5} {'ρ':>6}   앱: 예측 vs 실측")
    for o in obs:
        pred, rho = predict(profiles, o["traffic"], o["nodes"], usable, usable_iso)
        d = "  ".join(f"{a}: {pred[a]:.1f} vs {min(100.0,v):.1f}" for a,v in o["observed"].items() if a in pred)
        t = ",".join(f"{k}={v}" for k,v in o["traffic"].items())
        print(f"{t:>22} {o['nodes']:>5.2f} {rho:>6.2f}   {d}")
    json.dump({"usable": usable, "usable_iso": usable_iso}, open(os.path.join(HERE,"calibration.json"),"w"), indent=2)

if __name__ == "__main__":
    main()
