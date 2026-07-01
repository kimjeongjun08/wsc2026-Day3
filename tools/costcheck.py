"""
costcheck.py
현재 실행 중인 인스턴스를 실측해 "인스턴스 비용" 을 추정하고, 채점표의 비용 최적화
구간(cost ratio 0.50~3.75, 낮을수록 만점)에서 어디쯤인지 감을 잡기 위한 의사결정 보조 툴.

- 채점표의 정확한 ratio 분모(기준 비용)는 공개되지 않으므로, 이 툴은 선수의 실제 시간당
  인스턴스 비용(numerator)과 노드 수를 보여준다. --baseline <시간당달러> 를 주면 ratio 도 계산.
- 비용은 노드 수에 비례하므로, "얼마나 노드를 줄여도 되는지"를 판단하는 데 쓴다.
  (성능/가용성 게이트: 모든 API perf ≥30% 여야 비용 점수 인정 — 너무 줄이면 SLO 붕괴)

사용법:
  python costcheck.py                 # 현재 시간당 비용 + 노드 구성 출력
  python costcheck.py --baseline 0.30 # 기준 0.30$/h 대비 ratio 추정
  python costcheck.py --watch         # 30초마다 갱신
의존성: boto3 (update_waf.py 와 동일 환경), 표준 라이브러리.
가격은 ap-northeast-2 온디맨드 근사값(2026 기준, 변동 가능) — 상대 비교용.
"""
import sys
import time

import boto3

REGION = "ap-northeast-2"

# ap-northeast-2 On-Demand 시간당 근사 단가 (USD). 정확한 청구가 아니라 상대 비교용.
EC2_HOURLY = {
    "t3.micro": 0.0130,
    "t3.small": 0.0260,
    "t3.medium": 0.0520,
    "t3.large": 0.1040,
    "t3.xlarge": 0.2080,
    "m5.large": 0.1180,
    "m5.xlarge": 0.2360,
}
# RDS db.t3.micro (MySQL) 시간당 근사. Multi-AZ 는 ×2.
RDS_HOURLY = {"db.t3.micro": 0.026, "db.t3.small": 0.052}


def running_ec2():
    ec2 = boto3.client("ec2", region_name=REGION)
    types = {}
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(Filters=[{"Name": "instance-state-name", "Values": ["running"]}]):
        for r in page["Reservations"]:
            for i in r["Instances"]:
                t = i["InstanceType"]
                name = next((tag["Value"] for tag in i.get("Tags", []) if tag["Key"] == "Name"), "")
                types.setdefault(t, {"count": 0, "names": []})
                types[t]["count"] += 1
                types[t]["names"].append(name or i["InstanceId"])
    return types


def rds_instances():
    rds = boto3.client("rds", region_name=REGION)
    out = []
    for db in rds.describe_db_instances()["DBInstances"]:
        out.append({
            "id": db["DBInstanceIdentifier"],
            "class": db["DBInstanceClass"],
            "multi_az": db.get("MultiAZ", False),
            "status": db["DBInstanceStatus"],
        })
    return out


def report(baseline=None):
    ec2 = running_ec2()
    rds = rds_instances()

    print(f"\n=== Cost check ({REGION}) ===\n")
    ec2_cost = 0.0
    node_workers = 0
    print(" [EC2 running]")
    for t, info in sorted(ec2.items()):
        unit = EC2_HOURLY.get(t)
        sub = (unit or 0) * info["count"]
        ec2_cost += sub
        if t == "t3.medium":
            node_workers += info["count"]
        price = f"${unit:.4f}/h" if unit else "단가미상"
        print(f"  {t:<12} x{info['count']:<3} {price:>12} = ${sub:.4f}/h  [{', '.join(info['names'][:6])}]")

    rds_cost = 0.0
    print("\n [RDS]")
    for db in rds:
        unit = RDS_HOURLY.get(db["class"], 0)
        sub = unit * (2 if db["multi_az"] else 1)
        rds_cost += sub
        az = "Multi-AZ" if db["multi_az"] else "Single-AZ"
        print(f"  {db['id']:<22} {db['class']} {az} ({db['status']}) = ${sub:.4f}/h")

    total = ec2_cost + rds_cost
    print(f"\n {'-'*44}")
    print(f"  워커(t3.medium): {node_workers} 대")
    print(f"  EC2 합계   ${ec2_cost:.4f}/h")
    print(f"  RDS 합계   ${rds_cost:.4f}/h")
    print(f"  총계       ${total:.4f}/h")
    if baseline:
        ratio = total / baseline if baseline else 0
        band = "만점권(≤1.0)" if ratio <= 1.0 else (f"{ratio:.2f} (≤3.75 까지 점수)" if ratio <= 3.75 else "3.75 초과 → 비용 0점")
        print(f"  기준 ${baseline:.4f}/h 대비 ratio ≈ {ratio:.2f}  → {band}")
    else:
        print("  (--baseline <시간당달러> 주면 cost ratio 추정)")
    print()


def main():
    baseline = None
    watch = "--watch" in sys.argv
    if "--baseline" in sys.argv:
        try:
            baseline = float(sys.argv[sys.argv.index("--baseline") + 1])
        except (IndexError, ValueError):
            print("사용법: python costcheck.py [--baseline <시간당달러>] [--watch]")
            sys.exit(2)
    if watch:
        try:
            while True:
                print("\033[2J\033[H", end="")
                report(baseline)
                time.sleep(30)
        except KeyboardInterrupt:
            pass
    else:
        report(baseline)


if __name__ == "__main__":
    main()
