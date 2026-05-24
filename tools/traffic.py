#!/usr/bin/env python3
"""
onda-traffic: 트래픽 분석 (한 화면에 전부)

  python onda-traffic.py              # 최근 1시간
  python onda-traffic.py -t 3         # 최근 3시간
  python onda-traffic.py --logs       # 로그도 포함
  python onda-traffic.py --error      # 에러 로그만
  python onda-traffic.py --report     # md 보고서 생성
"""

import argparse, boto3, sys
from datetime import datetime, timedelta, timezone

R = "ap-northeast-2"
ALB = "app/onda-mart-alb/a7ddfe25dd431b46"
TG = "targetgroup/onda-mart-tg/7abf01c99830d1f7"
DB = "onda-mart-db"
CL, SV = "onda-mart", "onda-app"
KST = timezone(timedelta(hours=9))

cw = boto3.client("cloudwatch", region_name=R)
ecs = boto3.client("ecs", region_name=R)
lg = boto3.client("logs", region_name=R)

def g(ns, name, dims, stat="Sum", period=300, hours=1):
    r = cw.get_metric_statistics(
        Namespace=ns, MetricName=name,
        Dimensions=[{"Name":k,"Value":v} for k,v in dims.items()],
        StartTime=datetime.now(timezone.utc)-timedelta(hours=hours),
        EndTime=datetime.now(timezone.utc), Period=period, Statistics=[stat])
    return {p["Timestamp"].astimezone(KST).strftime("%H:%M"): p[stat]
            for p in r["Datapoints"]}

def f(n):
    if n>=1e6: return f"{n/1e6:.1f}M"
    if n>=1e3: return f"{n/1e3:.1f}K"
    return str(int(n))

def bar(v, mx, w=15):
    return "█"*int(v/mx*w)+"░"*(w-int(v/mx*w)) if mx else "░"*w

def run(hours, show_logs=False, error_only=False, report=False):
    A = {"LoadBalancer": ALB}
    AT = {"TargetGroup": TG, "LoadBalancer": ALB}
    D = {"DBInstanceIdentifier": DB}
    E = {"ClusterName": CL, "ServiceName": SV}
    h = hours

    # 전부 수집
    reqs   = g("AWS/ApplicationELB","RequestCount",A,hours=h)
    ok     = g("AWS/ApplicationELB","HTTPCode_Target_2XX_Count",A,hours=h)
    c4     = g("AWS/ApplicationELB","HTTPCode_Target_4XX_Count",A,hours=h)
    c5     = g("AWS/ApplicationELB","HTTPCode_Target_5XX_Count",A,hours=h)
    rt     = g("AWS/ApplicationELB","TargetResponseTime",A,"Average",hours=h)
    rt_mx  = g("AWS/ApplicationELB","TargetResponseTime",A,"Maximum",hours=h)
    conn   = g("AWS/ApplicationELB","ActiveConnectionCount",A,hours=h)
    hl     = g("AWS/ApplicationELB","HealthyHostCount",AT,"Average",hours=h)
    ecpu   = g("AWS/ECS","CPUUtilization",E,"Average",hours=h)
    emem   = g("AWS/ECS","MemoryUtilization",E,"Average",hours=h)
    rcpu   = g("AWS/RDS","CPUUtilization",D,"Average",hours=h)
    rcon   = g("AWS/RDS","DatabaseConnections",D,"Average",hours=h)
    rmem   = g("AWS/RDS","FreeableMemory",D,"Average",hours=h)
    rwio   = g("AWS/RDS","WriteIOPS",D,"Average",hours=h)
    rrio   = g("AWS/RDS","ReadIOPS",D,"Average",hours=h)

    times = sorted(reqs.keys())
    if not times:
        print("데이터 없음.")
        return

    svc = ecs.describe_services(cluster=CL, services=[SV])["services"][0]
    peak = max(reqs.values()) if reqs else 1

    # 헤더
    print(f"\n{'='*130}")
    print(f"  Onda-Mart 트래픽 분석 | 최근 {h}시간 | 태스크: run={svc['runningCount']} des={svc['desiredCount']} | {datetime.now(KST).strftime('%H:%M')} KST")
    print(f"{'='*130}")
    print(f"{'':>6} ┃{'── ALB ──────────────────────────────────────────────────────':^62}┃{'── ECS ──':^18}┃{'── RDS ──────────────────────────':^34}┃")
    print(f"{'시간':>6} ┃{'요청':>7} {'그래프':<17} {'2XX':>5} {'4XX':>5} {'5XX':>5} {'err%':>5} {'avg':>6} {'max':>6} {'연결':>4} {'H':>2}┃{'CPU':>6} {'MEM':>6} ┃{'CPU':>6} {'연결':>4} {'RAM':>6} {'W IO':>6} {'R IO':>6}┃")
    print(f"{'─'*6}─╋{'─'*62}╋{'─'*18}╋{'─'*34}╋")

    lines = []
    for t in times:
        rq = reqs.get(t,0)
        r2 = ok.get(t,0); r4 = c4.get(t,0); r5 = c5.get(t,0)
        er = f"{(r4+r5)/rq*100:.0f}%" if rq else "-"
        ra = f"{rt.get(t,0)*1000:.0f}ms" if t in rt else "-"
        rm = f"{rt_mx.get(t,0)*1000:.0f}ms" if t in rt_mx else "-"
        cn = f(conn.get(t,0)); hy = f"{hl.get(t,0):.0f}"
        ec = f"{ecpu.get(t,0):.0f}%" if t in ecpu else "-"
        em = f"{emem.get(t,0):.0f}%" if t in emem else "-"
        rc = f"{rcpu.get(t,0):.0f}%" if t in rcpu else "-"
        rn = f"{rcon.get(t,0):.0f}" if t in rcon else "-"
        mm = f"{rmem.get(t,0)/1024/1024:.0f}M" if t in rmem else "-"
        wi = f"{rwio.get(t,0):.0f}/s" if t in rwio else "-"
        ri = f"{rrio.get(t,0):.0f}/s" if t in rrio else "-"

        flag = ""
        if r5 > 100: flag = "🔴"
        elif rq and (r4+r5)/rq > 0.3: flag = "🟡"
        elif t in rcpu and rcpu[t] > 80: flag = "🔴"

        line = f"{t:>6} ┃{f(rq):>7} {bar(rq,peak):<17} {f(r2):>5} {f(r4):>5} {f(r5):>5} {er:>5} {ra:>6} {rm:>6} {cn:>4} {hy:>2}┃{ec:>6} {em:>6} ┃{rc:>6} {rn:>4} {mm:>6} {wi:>6} {ri:>6}┃{flag}"
        print(line)
        lines.append((t,rq,r2,r4,r5,rt.get(t,0),rt_mx.get(t,0),ecpu.get(t,0),emem.get(t,0),rcpu.get(t,0),rcon.get(t,0),rmem.get(t,0),rwio.get(t,0)))

    # 요약
    tr = sum(reqs.values()); t5 = sum(c5.values()); t4 = sum(c4.values())
    pk_t = max(times, key=lambda t: reqs.get(t,0))
    avg_rt = sum(rt.values())/len(rt)*1000 if rt else 0
    print(f"{'─'*6}─╋{'─'*62}╋{'─'*18}╋{'─'*34}╋")
    print(f"  합계 ┃ 요청:{f(tr)}  5XX:{f(t5)}  4XX:{f(t4)}  피크:{f(peak)}({pk_t})  평균응답:{avg_rt:.0f}ms")

    # 패턴 감지
    vals = [reqs.get(t,0) for t in times]
    avg_v = sum(vals)/len(vals) if vals else 0
    spikes = [(t,reqs[t]) for t in times if reqs.get(t,0) > avg_v*3 and reqs.get(t,0) > 100]
    anoms = [(t,reqs[t]) for t in times if reqs.get(t,0)>10 and (c4.get(t,0)+c5.get(t,0))/reqs[t]>0.3]

    if spikes or anoms:
        print(f"\n  ⚡ 스파이크: {len(spikes)}건  ⚠️ 비정상: {len(anoms)}건")
        for t,v in spikes[:3]:
            print(f"    {t} — {f(v)} ({v/avg_v:.1f}x 평균)")

    # 로그
    if show_logs:
        print(f"\n{'='*130}")
        print(f"  로그 {'(에러만)' if error_only else ''}")
        print(f"{'='*130}")
        params = {"logGroupName":"/ecs/onda-app",
                  "startTime":int((datetime.now(timezone.utc)-timedelta(hours=h)).timestamp()*1000),
                  "endTime":int(datetime.now(timezone.utc).timestamp()*1000),
                  "limit":30,"interleaved":True}
        if error_only:
            params["filterPattern"] = "?ERROR ?error ?Exception ?FATAL ?panic"
        try:
            for e in lg.filter_log_events(**params).get("events",[]):
                ts = datetime.fromtimestamp(e["timestamp"]/1000,tz=KST).strftime("%H:%M:%S")
                print(f"  [{ts}] {e['message'].strip()[:120]}")
        except: print("  로그 조회 실패")

    # 보고서
    if report:
        now = datetime.now(KST)
        path = f"C:\\Users\\admin\\onda-mart\\traffic-{now.strftime('%Y%m%d-%H%M')}.md"
        md = [f"# 트래픽 분석 — {now.strftime('%Y-%m-%d %H:%M')} KST (최근 {h}시간)","",
              f"태스크: run={svc['runningCount']} des={svc['desiredCount']}","",
              "| 시간 | 요청 | 2XX | 4XX | 5XX | err% | avg | max | ECS CPU | ECS MEM | RDS CPU | DB연결 | W IOPS |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for t,rq,r2,r4,r5,ra,rm,ec,em,rc,rn,rmm,wi in lines:
            er = f"{(r4+r5)/rq*100:.0f}%" if rq else "-"
            md.append(f"| {t} | {f(rq)} | {f(r2)} | {f(r4)} | {f(r5)} | {er} | {ra*1000:.0f}ms | {rm*1000:.0f}ms | {ec:.0f}% | {em:.0f}% | {rc:.0f}% | {rn:.0f} | {wi:.0f}/s |")
        with open(path,"w",encoding="utf-8") as fp: fp.write("\n".join(md))
        print(f"\n  📄 보고서: {path}")

def main():
    p = argparse.ArgumentParser(description="onda-traffic: 트래픽 분석")
    p.add_argument("-t","--hours", type=float, default=1, help="시간 범위")
    p.add_argument("--logs", action="store_true", help="로그 포함")
    p.add_argument("--error", action="store_true", help="에러 로그만")
    p.add_argument("--report", action="store_true", help="md 보고서 생성")
    args = p.parse_args()
    run(args.hours, args.logs, args.error, args.report)

if __name__ == "__main__":
    main()
