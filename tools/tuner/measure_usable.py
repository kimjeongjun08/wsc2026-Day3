#!/usr/bin/env python3
"""measure_usable.py — 노드의 '실효 CPU 비율 usable' 을 추측하지 않고 측정한다.

usable 의 정의:
    ρ = 앱 CPU 수요 / (노드수 × vCPU × usable)
즉 vCPU 중 앱 처리에 실제로 돌아가는 몫이다. 나머지는 kubelet·CNI·containerd·
컨트롤러 같은 시스템 몫이라 앱이 쓸 수 없다.

측정 방법:
    회차 스파이크 구간에서
      · 앱 CPU 수요 D  = Σ (요청률 × 프로파일 d)            ← 프로파일에서
      · 노드 실사용률 U = CloudWatch CPUUtilization 평균      ← 실측
    노드가 실제로 쓴 CPU 는 노드수×vCPU×U 이고 그중 앱 몫이 D 이므로
      usable = D / (노드수 × vCPU × U)

왜 이렇게 하나: 예전엔 회차 점수에 맞춰 usable 을 격자탐색으로 끼워 맞췄다.
그러면 모델의 다른 오차까지 이 계수가 떠안아서(0.24 까지 내려갔다) 물리적 의미가
사라진다. 측정값과 적합값이 벌어지면 그 차이가 곧 '모델이 아직 설명 못 하는 몫'이다.

사용:
    python3 measure_usable.py --traffic '{"user":22,"product":24,"stress":1.25}' \\
        --nodes 2 --minutes 10
"""
import json, argparse, os, subprocess, datetime

HERE = os.path.dirname(os.path.abspath(__file__))

def cw_avg_utilization(instance_ids, start, end, region, profile):
    vals = []
    for iid in instance_ids:
        out = subprocess.run(
            ["aws", "cloudwatch", "get-metric-statistics", "--region", region,
             "--namespace", "AWS/EC2", "--metric-name", "CPUUtilization",
             "--dimensions", f"Name=InstanceId,Value={iid}",
             "--start-time", start, "--end-time", end,
             "--period", "300", "--statistics", "Average",
             "--query", "Datapoints[].Average", "--output", "json"],
            capture_output=True, text=True,
            env={**os.environ, "AWS_PROFILE": profile})
        try:
            pts = json.loads(out.stdout or "[]")
        except json.JSONDecodeError:
            pts = []
        if pts:
            vals.append(sum(pts) / len(pts))
    return (sum(vals) / len(vals) / 100.0) if vals else None

def worker_instance_ids(region, profile):
    out = subprocess.run(
        ["aws", "ec2", "describe-instances", "--region", region,
         "--filters", "Name=instance-state-name,Values=running",
         "Name=instance-type,Values=t3.medium",
         "--query", "Reservations[].Instances[].InstanceId", "--output", "json"],
        capture_output=True, text=True, env={**os.environ, "AWS_PROFILE": profile})
    try:
        return json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return []

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--traffic", required=True)
    ap.add_argument("--nodes", type=int, required=True)
    ap.add_argument("--vcpu", type=float, default=2.0)
    ap.add_argument("--minutes", type=int, default=10, help="직전 몇 분을 볼지")
    ap.add_argument("--start", help="UTC 시작 (예: 2026-08-19T02:29:00). 스파이크 구간을 정확히 집을 때")
    ap.add_argument("--end", help="UTC 종료")
    ap.add_argument("--region", default="ap-northeast-2")
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE", "lee"))
    ap.add_argument("--write", action="store_true", help="calibration.json 에 반영")
    a = ap.parse_args()

    traffic = json.loads(a.traffic)
    demand = 0.0
    for app, rps in traffic.items():
        f = os.path.join(HERE, f"profile-{app}.json")
        if not os.path.exists(f):
            print(f"[skip] profile-{app}.json 없음"); continue
        d = json.load(open(f))["cpu_ms_mean"] / 1000.0
        demand += rps * d
        print(f"   {app:>8}: {rps:>6.2f} rps × {d*1000:>6.2f} ms = {rps*d:.3f} core")
    print(f"   앱 CPU 수요 D = {demand:.3f} core")

    fmt = "%Y-%m-%dT%H:%M:%S"
    if a.start and a.end:
        start_s, end_s = a.start, a.end
    else:
        end = datetime.datetime.now(datetime.timezone.utc)
        start = end - datetime.timedelta(minutes=a.minutes)
        start_s, end_s = start.strftime(fmt), end.strftime(fmt)
    ids = worker_instance_ids(a.region, a.profile)
    util = cw_avg_utilization(ids, start_s, end_s, a.region, a.profile)
    if util is None:
        raise SystemExit("CloudWatch 데이터가 없다 — 회차 직후에 돌려라")
    print(f"   노드 {len(ids)}대 실사용률 U = {util*100:.1f}%  ({start_s}~{end_s} UTC)")

    usable = demand / (a.nodes * a.vcpu * util)
    print(f"\n측정된 usable = D / (노드수 × vCPU × U) = {usable:.3f}")
    print("   → 1.0 에 가까울수록 시스템 오버헤드가 작다는 뜻이다.")

    if a.write:
        cal_f = os.path.join(HERE, "calibration.json")
        cal = json.load(open(cal_f)) if os.path.exists(cal_f) else {}
        cal["usable_measured"] = round(usable, 3)
        json.dump(cal, open(cal_f, "w"), indent=2)
        print(f"   calibration.json 에 usable_measured 로 기록했다.")

if __name__ == "__main__":
    main()
