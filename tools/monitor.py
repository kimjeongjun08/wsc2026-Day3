#!/usr/bin/env python3
import subprocess
import json
import sys
import time
import threading
import curses
import asciichartpy
from datetime import datetime, timedelta
from collections import deque

sys.stdout.reconfigure(encoding='utf-8')

REGION = "ap-northeast-2"
config = {"lb_arn": "", "user_tg": "", "product_tg": "", "stress_tg": "",
          "rds_id": "apdev-rds-instance"}

cache = {"metrics": {}, "ts": 0}
history = deque(maxlen=240)  # 4분 x 60 = 240 (15초 간격이면 1시간)


def cw_get(namespace, metric, dimensions, stat="Average", period=60):
    end = datetime.utcnow()
    start = end - timedelta(minutes=5)
    dim_args = []
    for k, v in dimensions.items():
        dim_args.extend(["--dimensions", f"Name={k},Value={v}"])
    cmd = ["aws", "cloudwatch", "get-metric-statistics",
           "--namespace", namespace, "--metric-name", metric,
           "--start-time", start.strftime("%Y-%m-%dT%H:%M:%S"),
           "--end-time", end.strftime("%Y-%m-%dT%H:%M:%S"),
           "--period", str(period), "--statistics", stat,
           "--region", REGION] + dim_args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        pts = sorted(json.loads(r.stdout).get("Datapoints", []), key=lambda x: x["Timestamp"])
        return pts[-1].get(stat, 0) if pts else 0
    except:
        return 0


def fetch_loop():
    while True:
        lb = config["lb_arn"]
        if not lb:
            time.sleep(2)
            continue
        m = {}
        m["rds_cpu"] = cw_get("AWS/RDS", "CPUUtilization", {"DBInstanceIdentifier": config["rds_id"]})
        m["rds_conn"] = cw_get("AWS/RDS", "DatabaseConnections", {"DBInstanceIdentifier": config["rds_id"]}, "Sum")
        m["alb_req"] = cw_get("AWS/ApplicationELB", "RequestCount", {"LoadBalancer": lb}, "Sum")
        m["alb_4xx"] = cw_get("AWS/ApplicationELB", "HTTPCode_ELB_4XX_Count", {"LoadBalancer": lb}, "Sum")
        m["alb_5xx"] = cw_get("AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", {"LoadBalancer": lb}, "Sum")
        m["tgt_5xx"] = cw_get("AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", {"LoadBalancer": lb}, "Sum")
        for name, tg in [("user", config["user_tg"]), ("product", config["product_tg"]), ("stress", config["stress_tg"])]:
            if tg:
                m[f"{name}_rt"] = cw_get("AWS/ApplicationELB", "TargetResponseTime", {"TargetGroup": tg, "LoadBalancer": lb})
                m[f"{name}_req"] = cw_get("AWS/ApplicationELB", "RequestCount", {"TargetGroup": tg, "LoadBalancer": lb}, "Sum")
        cache["metrics"] = m
        cache["ts"] = time.time()
        history.append({"ts": time.time(), **m})
        time.sleep(15)


def safe_addstr(stdscr, y, x, text, *args):
    try:
        if 0 <= y:
            stdscr.addstr(y, x, str(text)[:200], *args)
    except curses.error:
        pass


def render_chart(data, width, height):
    if not data or all(v == 0 for v in data):
        return ["  (no data)"] * height
    trimmed = list(data)[-width:]
    try:
        chart = asciichartpy.plot(trimmed, {"height": height, "width": width, "format": "{:6.0f}"})
        return chart.split("\n")
    except:
        return ["  (chart error)"] * height


def draw_metrics_tab(stdscr, height, width):
    m = cache.get("metrics", {})
    age = int(time.time() - cache["ts"]) if cache["ts"] else 999
    hist = list(history)
    chart_w = min(50, (width - 10) // 2)
    chart_h = 8

    y = 2

    # ── RDS CPU Graph ──
    safe_addstr(stdscr, y, 1, f"RDS CPU % (current: {m.get('rds_cpu', 0):.1f}%)", curses.A_BOLD | curses.color_pair(1))
    safe_addstr(stdscr, y, chart_w + 15, f"ALB Requests/min (current: {m.get('alb_req', 0):.0f})", curses.A_BOLD | curses.color_pair(2))
    y += 1

    cpu_data = [h.get("rds_cpu", 0) for h in hist]
    req_data = [h.get("alb_req", 0) for h in hist]
    cpu_lines = render_chart(cpu_data, chart_w, chart_h)
    req_lines = render_chart(req_data, chart_w, chart_h)

    for i in range(max(len(cpu_lines), len(req_lines))):
        if i < len(cpu_lines):
            safe_addstr(stdscr, y + i, 1, cpu_lines[i], curses.color_pair(1))
        if i < len(req_lines):
            safe_addstr(stdscr, y + i, chart_w + 15, req_lines[i], curses.color_pair(2))

    y += chart_h + 2

    # ── Response Time Graph ──
    safe_addstr(stdscr, y, 1, "Response Time ms (user/product/stress)", curses.A_BOLD | curses.color_pair(3))
    safe_addstr(stdscr, y, chart_w + 15, "Errors (5xx)", curses.A_BOLD | curses.color_pair(4))
    y += 1

    # 가장 높은 RT 앱 그래프
    user_rt = [h.get("user_rt", 0) * 1000 for h in hist]
    prod_rt = [h.get("product_rt", 0) * 1000 for h in hist]
    stress_rt = [h.get("stress_rt", 0) * 1000 for h in hist]
    # stress가 보통 가장 높으니 stress 기준
    rt_lines = render_chart(stress_rt, chart_w, chart_h)
    err_data = [h.get("alb_5xx", 0) + h.get("tgt_5xx", 0) for h in hist]
    err_lines = render_chart(err_data, chart_w, chart_h)

    for i in range(max(len(rt_lines), len(err_lines))):
        if i < len(rt_lines):
            safe_addstr(stdscr, y + i, 1, rt_lines[i], curses.color_pair(3))
        if i < len(err_lines):
            safe_addstr(stdscr, y + i, chart_w + 15, err_lines[i], curses.color_pair(4))

    y += chart_h + 2

    # ── Summary Table ──
    safe_addstr(stdscr, y, 1, "CURRENT VALUES", curses.A_BOLD)
    y += 1
    safe_addstr(stdscr, y, 1, f"{'Metric':<25}{'Value':>10}  {'Status'}")
    y += 1
    safe_addstr(stdscr, y, 1, "-" * 50)
    y += 1

    rows = [
        ("RDS CPU", f"{m.get('rds_cpu',0):.1f}%", m.get('rds_cpu',0) < 80),
        ("RDS Connections", f"{m.get('rds_conn',0):.0f}", True),
        ("ALB Requests/min", f"{m.get('alb_req',0):.0f}", True),
        ("ALB 4xx", f"{m.get('alb_4xx',0):.0f}", m.get('alb_4xx',0) == 0),
        ("ALB 5xx", f"{m.get('alb_5xx',0):.0f}", m.get('alb_5xx',0) == 0),
        ("user RT", f"{m.get('user_rt',0)*1000:.0f}ms", m.get('user_rt',0)*1000 < 200),
        ("product RT", f"{m.get('product_rt',0)*1000:.0f}ms", m.get('product_rt',0)*1000 < 200),
        ("stress RT", f"{m.get('stress_rt',0)*1000:.0f}ms", m.get('stress_rt',0)*1000 < 1000),
    ]
    for label, val, ok in rows:
        c = curses.color_pair(5) if ok else curses.color_pair(4)
        mark = "OK" if ok else "!!"
        safe_addstr(stdscr, y, 1, f"  {label:<23}{val:>10}  [{mark}]", c)
        y += 1


def draw_traffic_tab(stdscr, height, width):
    hist = list(history)
    y = 2

    if len(hist) < 3:
        safe_addstr(stdscr, y, 1, "  Collecting data... (need at least 3 samples)", curses.color_pair(2))
        return

    chart_w = min(60, width - 15)
    chart_h = 10

    # ── Traffic Graph ──
    safe_addstr(stdscr, y, 1, "TRAFFIC TIMELINE (req/min)", curses.A_BOLD)
    y += 1
    totals = [h.get("alb_req", 0) for h in hist]
    lines = render_chart(totals, chart_w, chart_h)
    for line in lines:
        safe_addstr(stdscr, y, 1, line, curses.color_pair(5))
        y += 1
    y += 1

    # ── Statistics Table ──
    safe_addstr(stdscr, y, 1, "STATISTICS", curses.A_BOLD)
    y += 1

    current = totals[-1]
    peak = max(totals)
    avg = sum(totals) / len(totals)
    minimum = min(totals)
    total_reqs = sum(totals)
    duration_min = len(totals) * 15 / 60  # 15초 간격

    safe_addstr(stdscr, y, 1, f"  {'Current:':<15}{current:>8.0f} req/min")
    safe_addstr(stdscr, y, 35, f"{'Peak:':<15}{peak:>8.0f} req/min")
    y += 1
    safe_addstr(stdscr, y, 1, f"  {'Average:':<15}{avg:>8.0f} req/min")
    safe_addstr(stdscr, y, 35, f"{'Min:':<15}{minimum:>8.0f} req/min")
    y += 1
    safe_addstr(stdscr, y, 1, f"  {'Total reqs:':<15}{total_reqs:>8.0f}")
    safe_addstr(stdscr, y, 35, f"{'Duration:':<15}{duration_min:>7.1f} min")
    y += 2

    # ── Trend Analysis ──
    safe_addstr(stdscr, y, 1, "TREND ANALYSIS", curses.A_BOLD)
    y += 1

    # 구간 분석 (5개 구간으로 나눔)
    segments = min(5, len(totals) // 3)
    seg_size = len(totals) // segments if segments > 0 else len(totals)

    safe_addstr(stdscr, y, 1, f"  {'Segment':<12}{'Avg':>8}{'Peak':>8}{'Trend':>8}")
    y += 1
    safe_addstr(stdscr, y, 1, "  " + "-" * 40)
    y += 1

    prev_avg = 0
    for i in range(segments):
        seg = totals[i * seg_size:(i + 1) * seg_size]
        if not seg:
            continue
        seg_avg = sum(seg) / len(seg)
        seg_peak = max(seg)
        if prev_avg > 0:
            change = (seg_avg - prev_avg) / prev_avg * 100
            arrow = f"+{change:.0f}%" if change > 0 else f"{change:.0f}%"
            c = curses.color_pair(4) if change > 30 else curses.color_pair(2) if change > 0 else curses.color_pair(1)
        else:
            arrow = "-"
            c = 0
        safe_addstr(stdscr, y, 1, f"  {f'#{i+1}':<12}{seg_avg:>8.0f}{seg_peak:>8.0f}")
        safe_addstr(stdscr, y, 35, f"{arrow:>8}", c)
        prev_avg = seg_avg
        y += 1

    y += 1

    # ── Pattern Detection ──
    safe_addstr(stdscr, y, 1, "PATTERN", curses.A_BOLD)
    y += 1

    recent = totals[-5:]
    older = totals[-15:-5] if len(totals) >= 15 else totals[:5]
    avg_r = sum(recent) / len(recent) if recent else 0
    avg_o = sum(older) / len(older) if older else 1

    ratio = avg_r / max(avg_o, 1)
    if ratio > 2:
        safe_addstr(stdscr, y, 1, "  [SPIKE] Sudden traffic surge detected!", curses.color_pair(4))
    elif ratio > 1.3:
        safe_addstr(stdscr, y, 1, "  [RAMP UP] Traffic gradually increasing", curses.color_pair(2))
    elif ratio < 0.5:
        safe_addstr(stdscr, y, 1, "  [DROP] Traffic dropping significantly", curses.color_pair(1))
    elif ratio < 0.8:
        safe_addstr(stdscr, y, 1, "  [COOLING] Traffic decreasing", curses.color_pair(1))
    else:
        safe_addstr(stdscr, y, 1, "  [STEADY] Traffic stable", curses.color_pair(5))
    y += 1

    # 에러율 추이
    errs = [h.get("alb_5xx", 0) + h.get("tgt_5xx", 0) for h in hist]
    total_err = sum(errs)
    err_rate = total_err / max(total_reqs, 1) * 100
    c = curses.color_pair(4) if err_rate > 1 else curses.color_pair(5)
    safe_addstr(stdscr, y, 1, f"  Error rate: {err_rate:.3f}% ({total_err:.0f}/{total_reqs:.0f})", c)
    y += 1

    # 앱별 비율
    y += 1
    safe_addstr(stdscr, y, 1, "APP DISTRIBUTION", curses.A_BOLD)
    y += 1
    cur_total = current if current > 0 else 1
    for name, cp in [("user", 1), ("product", 2), ("stress", 3)]:
        rq = cache.get("metrics", {}).get(f"{name}_req", 0)
        pct = rq / cur_total * 100
        bar = "#" * int(pct / 3) + "." * (33 - int(pct / 3))
        safe_addstr(stdscr, y, 1, f"  {name:<8}", curses.color_pair(cp))
        safe_addstr(stdscr, y, 11, f"[{bar}] {pct:.1f}% ({rq:.0f})")
        y += 1


def draw(stdscr):
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_MAGENTA, -1)
    curses.init_pair(4, curses.COLOR_RED, -1)
    curses.init_pair(5, curses.COLOR_GREEN, -1)
    curses.init_pair(6, curses.COLOR_WHITE, -1)
    curses.curs_set(0)
    mode = 0

    while True:
        stdscr.timeout(500)
        key = stdscr.getch()
        if key == ord('q'):
            break
        elif key == ord('1'):
            mode = 0
        elif key == ord('2'):
            mode = 1
        elif key == 9:
            mode = (mode + 1) % 2

        height, width = stdscr.getmaxyx()
        stdscr.erase()

        age = int(time.time() - cache["ts"]) if cache["ts"] else 999
        tabs = ["[1]Metrics", "[2]Traffic"]
        tabs[mode] = f">{tabs[mode]}"
        ts = datetime.now().strftime("%H:%M:%S")
        safe_addstr(stdscr, 0, 1, f" Monitor | {'  '.join(tabs)} | {ts} | data:{age}s ago | q=quit "[:width-1], curses.A_BOLD)

        if mode == 0:
            draw_metrics_tab(stdscr, height, width)
        else:
            draw_traffic_tab(stdscr, height, width)

        stdscr.refresh()


def prompt_config():
    print("\n=== Monitor Setup ===\n")
    lb = input("  ALB ARN suffix (Enter=auto): ").strip()
    if not lb:
        try:
            r = subprocess.run(["aws", "elbv2", "describe-load-balancers", "--query",
                               "LoadBalancers[0].LoadBalancerArn", "--output", "text", "--region", REGION],
                              capture_output=True, text=True, timeout=5)
            full = r.stdout.strip()
            lb = full.split("loadbalancer/")[1] if "loadbalancer/" in full else ""
            print(f"  -> {lb}")
        except:
            lb = input("  Enter manually: ").strip()
    config["lb_arn"] = lb

    try:
        r = subprocess.run(["aws", "elbv2", "describe-target-groups", "--query",
                           "TargetGroups[*].[TargetGroupName,TargetGroupArn]", "--output", "json", "--region", REGION],
                          capture_output=True, text=True, timeout=5)
        for name, arn in json.loads(r.stdout):
            suffix = "targetgroup/" + arn.split(":targetgroup/")[1] if ":targetgroup/" in arn else ""
            if "user" in name:
                config["user_tg"] = suffix
            elif "product" in name:
                config["product_tg"] = suffix
            elif "stress" in name:
                config["stress_tg"] = suffix
        print(f"  TGs detected: {sum(1 for k in ['user_tg','product_tg','stress_tg'] if config[k])}/3")
    except:
        pass

    rds = input(f"  RDS ID [{config['rds_id']}]: ").strip()
    if rds:
        config["rds_id"] = rds
    print("\n  Starting...\n")
    time.sleep(1)


if __name__ == "__main__":
    prompt_config()
    threading.Thread(target=fetch_loop, daemon=True).start()
    curses.wrapper(draw)
