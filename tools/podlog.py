#!/usr/bin/env python3
import subprocess
import threading
import sys
import os
import re
import time
import curses
from collections import deque, defaultdict
from datetime import datetime

NAMESPACE = "apdev"
APPS = ["user", "product", "stress"]
MAX_LINES = 200

SLO = {
    "user":    {"target_ms": 200, "max_ms": 5000},
    "product": {"target_ms": 200, "max_ms": 5000},
    "stress":  {"target_ms": 1000, "max_ms": 5000},
}


def get_pods():
    result = subprocess.run(
        ["kubectl", "get", "pods", "-n", NAMESPACE, "-o",
         "custom-columns=NAME:.metadata.name,APP:.metadata.labels.app", "--no-headers"],
        capture_output=True, text=True
    )
    pods = []
    for line in result.stdout.strip().split("\n"):
        if line.strip():
            parts = line.split()
            if len(parts) >= 2:
                pods.append((parts[0], parts[1]))
    return pods


def parse_gin_line(line):
    import json as _json
    # Try JSON format: {"status":200,"dur_ms":109,"method":"GET","path":"/v1/user?..."}
    try:
        start = line.find('{')
        if start >= 0:
            obj = _json.loads(line[start:])
            status = next((obj[k] for k in ("status","status_code","code") if obj.get(k) is not None), None)
            ms = next((obj[k] for k in ("dur_ms","duration_ms","latency_ms","latency") if obj.get(k) is not None), None)
            method = obj.get("method")
            path = obj.get("path") or obj.get("uri") or obj.get("url")
            if path:
                path = path.split("?")[0]
            if status is not None:
                status = int(status)
                # dur might be in seconds (float < 1 typically)
                if ms is not None:
                    ms = float(ms)
                return status, ms, path, method
    except (ValueError, TypeError, KeyError):
        pass

    # Fallback: Gin text format  | 200 | 3.5ms |
    m = re.search(r'\|\s*(\d+)\s*\|\s*([\d.]+)(ms|µs|s)\s*\|', line)
    path_m = re.search(r'(GET|POST|PUT|DELETE|PATCH)\s+"([^"]+)"', line)
    path = path_m.group(2).split("?")[0] if path_m else None
    method = path_m.group(1) if path_m else None
    if m:
        status = int(m.group(1))
        val = float(m.group(2))
        unit = m.group(3)
        if unit == "ms":
            ms = val
        elif unit == "µs":
            ms = val / 1000
        else:
            ms = val * 1000
        return status, ms, path, method
    return None, None, path, method


def format_line(line, raw_mode=False):
    if "/healthcheck" in line:
        return None
    if raw_mode:
        return line
    line = re.sub(r'\[GIN\]\s*\d{4}/\d{2}/\d{2}\s*-\s*\d{2}:\d{2}:\d{2}\s*', '', line)
    gin_match = re.search(r'([\d.]+)(ms|µs)', line)
    if gin_match:
        val = float(gin_match.group(1))
        unit = gin_match.group(2)
        sec = val / 1000 if unit == "ms" else val / 1000000
        line = re.sub(r'[\d.]+(ms|µs)', f'{sec:.3f}s', line)
    return line


def calc_percentile(arr, p):
    if not arr:
        return 0
    s = sorted(arr)
    return s[min(int(len(s) * p / 100), len(s) - 1)]


def stream_to_buffer(pod_name, app, buffers, stats, lock):
    proc = subprocess.Popen(
        ["kubectl", "logs", "-f", "--tail", "50", pod_name, "-n", NAMESPACE],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        status, latency_ms, path, method = parse_gin_line(line)
        formatted = format_line(line)
        if formatted:
            kst = datetime.utcnow()
            kst = kst.replace(hour=(kst.hour + 9) % 24)
            ts = kst.strftime("%H:%M:%S")
            with lock:
                buffers[app].append(f"{ts} {formatted}")
                buffers[f"{app}_raw"].append(line)
                if status:
                    stats[app]["total"] += 1
                    if status >= 500:
                        stats[app]["5xx"] += 1
                    elif status >= 400:
                        stats[app]["4xx"] += 1
                    elif status >= 200:
                        stats[app]["2xx"] += 1
                    key = f"{method} {path}" if method and path else path or "unknown"
                    stats[app]["paths"][key]["total"] += 1
                    if status >= 500:
                        stats[app]["paths"][key]["5xx"] += 1
                    elif status >= 400:
                        stats[app]["paths"][key]["4xx"] += 1
                    else:
                        stats[app]["paths"][key]["2xx"] += 1
                    if latency_ms is not None:
                        if "lats" not in stats[app]["paths"][key]:
                            stats[app]["paths"][key]["lats"] = deque(maxlen=200)
                        stats[app]["paths"][key]["lats"].append(latency_ms)
                    if latency_ms is not None:
                        stats[app]["latencies"].append(latency_ms)
                        if latency_ms > SLO[app]["target_ms"]:
                            stats[app]["slo_breach"] += 1


def safe_addstr(stdscr, y, x, text, *args):
    try:
        if y >= 0:
            stdscr.addstr(y, x, text, *args)
    except curses.error:
        pass


def draw(stdscr, buffers, stats, lock):
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_MAGENTA, -1)
    curses.init_pair(4, curses.COLOR_RED, -1)
    curses.init_pair(5, curses.COLOR_GREEN, -1)
    curses.init_pair(6, curses.COLOR_WHITE, -1)
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)

    C = {"user": 1, "product": 2, "stress": 3}
    mode = 0
    log_focus = -1  # -1=all, 0=user, 1=product, 2=stress
    log_scroll = 0  # 0=latest(bottom), >0=scrolled up
    log_paused = False

    while True:
        stdscr.timeout(300 if mode in (0, 1, 2) else 2000)
        key = stdscr.getch()
        if key == ord('q'):
            break
        elif key == ord('1'):
            mode = 0
        elif key == ord('2'):
            mode = 1; log_scroll = 0; log_paused = False
        elif key == ord('3'):
            mode = 2; log_scroll = 0; log_paused = False
        elif key == ord('4'):
            mode = 3
        elif key == 9:
            mode = (mode + 1) % 4
        elif key == curses.KEY_RIGHT and mode in (1, 2):
            log_focus = (log_focus + 1) % len(APPS) if log_focus < len(APPS) - 1 else -1
            log_scroll = 0
        elif key == curses.KEY_LEFT and mode in (1, 2):
            log_focus = len(APPS) - 1 if log_focus == -1 else log_focus - 1
            log_scroll = 0
        elif key == curses.KEY_UP and mode in (1, 2):
            log_scroll += 3
            log_paused = True
        elif key == curses.KEY_DOWN and mode in (1, 2):
            log_scroll = max(0, log_scroll - 3)
            if log_scroll == 0:
                log_paused = False
        elif key == ord(' ') and mode in (1, 2):
            log_paused = not log_paused
            if not log_paused:
                log_scroll = 0

        height, width = stdscr.getmaxyx()
        stdscr.erase()

        tabs = ["[1]Dashboard", "[2]Logs", "[3]Raw", "[4]Pods"]
        tabs[mode] = f">{tabs[mode]}"
        hint = " ←→:focus ↑↓:scroll Space:pause" if mode in (1, 2) else ""
        safe_addstr(stdscr, 0, 1, f" Pod Monitor | {'  '.join(tabs)} | q=quit{hint} "[:width-1], curses.A_BOLD)

        if mode == 0:
            draw_dashboard(stdscr, stats, lock, height, width, C)
        elif mode == 1:
            draw_logs(stdscr, buffers, lock, height, width, C, log_focus, log_scroll, log_paused)
        elif mode == 2:
            draw_raw(stdscr, buffers, lock, height, width, C, log_focus, log_scroll, log_paused)
        else:
            draw_pods(stdscr, height, width, C)

        stdscr.refresh()


def draw_dashboard(stdscr, stats, lock, height, width, C):
    with lock:
        data = {app: dict(stats[app]) for app in APPS}
        lats = {app: list(stats[app]["latencies"]) for app in APPS}
        paths = {app: dict(stats[app]["paths"]) for app in APPS}

    mid_x = width // 2
    mid_y = (height - 4) // 2 + 2

    # ┌─────────────────────┬─────────────────────┐
    # │  STATUS + DIST      │  LATENCY            │
    # ├─────────────────────┼─────────────────────┤
    # │  API PATHS          │  SLO + NOTICE       │
    # └─────────────────────┴─────────────────────┘

    # Borders
    for y in range(1, height - 1):
        safe_addstr(stdscr, y, mid_x, "|")
    for x in range(width - 1):
        safe_addstr(stdscr, 1, x, "-")
        safe_addstr(stdscr, mid_y, x, "-")
    safe_addstr(stdscr, 1, mid_x, "+")
    safe_addstr(stdscr, mid_y, mid_x, "+")

    # ── TOP LEFT: Status + Distribution ──
    y = 2
    safe_addstr(stdscr, y, 1, "STATUS", curses.A_BOLD)
    y += 1
    safe_addstr(stdscr, y, 1, f"{'':8}{'user':>7}{'prod':>7}{'stress':>7}")
    y += 1
    for label, key, cp in [("2xx", "2xx", 5), ("4xx", "4xx", 2), ("5xx", "5xx", 4), ("Total", "total", 6)]:
        safe_addstr(stdscr, y, 1, f"{label:<8}")
        for i, app in enumerate(APPS):
            v = data[app].get(key, 0)
            safe_addstr(stdscr, y, 9 + i * 7, f"{v:>6}", curses.color_pair(cp) if v > 0 and key != "total" else 0)
        y += 1

    y += 1
    safe_addstr(stdscr, y, 1, "DISTRIBUTION", curses.A_BOLD)
    y += 1
    bar_w = min(20, mid_x - 12)
    for app in APPS:
        total = data[app].get("total", 0) or 1
        b2 = int(data[app].get("2xx", 0) / total * bar_w)
        b4 = int(data[app].get("4xx", 0) / total * bar_w)
        b5 = int(data[app].get("5xx", 0) / total * bar_w)
        safe_addstr(stdscr, y, 1, f"{app:<8}", curses.color_pair(C[app]))
        safe_addstr(stdscr, y, 9, "█" * b2, curses.color_pair(5))
        safe_addstr(stdscr, y, 9 + b2, "█" * b4, curses.color_pair(2))
        safe_addstr(stdscr, y, 9 + b2 + b4, "█" * b5, curses.color_pair(4))
        safe_addstr(stdscr, y, 9 + b2 + b4 + b5, "░" * max(0, bar_w - b2 - b4 - b5))
        y += 1

    # ── TOP RIGHT: Latency ──
    y = 2
    rx = mid_x + 2
    safe_addstr(stdscr, y, rx, "LATENCY", curses.A_BOLD)
    y += 1
    safe_addstr(stdscr, y, rx, f"{'':6}{'user':>8}{'prod':>8}{'stress':>8}")
    y += 1
    for label, pct in [("p50", 50), ("p95", 95), ("p99", 99), ("max", 100)]:
        safe_addstr(stdscr, y, rx, f"{label:<6}")
        for i, app in enumerate(APPS):
            v = calc_percentile(lats[app], pct)
            c = curses.color_pair(4) if v > SLO[app]["target_ms"] else curses.color_pair(5)
            safe_addstr(stdscr, y, rx + 6 + i * 8, f"{v:>6.0f}ms", c)
        y += 1

    y += 1
    safe_addstr(stdscr, y, rx, "p95 vs SLO", curses.A_BOLD)
    y += 1
    lat_bar_w = min(25, width - mid_x - 20)
    for app in APPS:
        p95 = calc_percentile(lats[app], 95)
        target = SLO[app]["target_ms"]
        filled = min(int(p95 / 5000 * lat_bar_w), lat_bar_w)
        c = curses.color_pair(4) if p95 > target else curses.color_pair(5)
        mark = "✓" if p95 <= target else "✗"
        safe_addstr(stdscr, y, rx, f"{app:<8}", curses.color_pair(C[app]))
        safe_addstr(stdscr, y, rx + 8, "█" * filled, c)
        safe_addstr(stdscr, y, rx + 8 + filled, "░" * (lat_bar_w - filled))
        safe_addstr(stdscr, y, rx + 9 + lat_bar_w, f"{p95:.0f}ms {mark}")
        y += 1

    # ── BOTTOM LEFT: API Paths + Latency Top ──
    y = mid_y + 1
    safe_addstr(stdscr, y, 1, "API PATHS (by p95 latency)", curses.A_BOLD)
    y += 1
    safe_addstr(stdscr, y, 1, f"{'Path':<20}{'Tot':>5}{'2xx':>5}{'4xx':>5}{'5xx':>5}{'p95':>7}")
    y += 1

    all_paths = []
    for app in APPS:
        for p, counts in paths[app].items():
            if isinstance(counts, dict):
                p95 = calc_percentile(list(counts.get("lats", [])), 95)
                all_paths.append((app, p, counts, p95))
    all_paths.sort(key=lambda x: x[3], reverse=True)

    max_rows = height - mid_y - 4
    for app, path, counts, p95 in all_paths[:min(10, max_rows)]:
        safe_addstr(stdscr, y, 1, f"{path:<20}"[:19], curses.color_pair(C[app]))
        safe_addstr(stdscr, y, 21, f"{counts['total']:>5}")
        safe_addstr(stdscr, y, 26, f"{counts['2xx']:>5}", curses.color_pair(5))
        safe_addstr(stdscr, y, 31, f"{counts['4xx']:>5}", curses.color_pair(2) if counts['4xx'] else 0)
        safe_addstr(stdscr, y, 36, f"{counts['5xx']:>5}", curses.color_pair(4) if counts['5xx'] else 0)
        c = curses.color_pair(4) if p95 > 200 else curses.color_pair(5)
        safe_addstr(stdscr, y, 41, f"{p95:>5.0f}ms", c)
        y += 1

    # ── BOTTOM RIGHT: SLO + Notice ──
    y = mid_y + 1
    rx = mid_x + 2
    safe_addstr(stdscr, y, rx, "SLO / SLI", curses.A_BOLD)
    y += 1
    for app in APPS:
        total = data[app].get("total", 0)
        breach = data[app].get("slo_breach", 0)
        s5 = data[app].get("5xx", 0)
        sli = ((total - breach) / total * 100) if total > 0 else 100
        avail = ((total - s5) / total * 100) if total > 0 else 100
        c = curses.color_pair(5) if sli >= 95 else curses.color_pair(4)
        safe_addstr(stdscr, y, rx, f"{app:<8}", curses.color_pair(C[app]))
        safe_addstr(stdscr, y, rx + 8, f"SLI:{sli:.1f}%", c)
        c2 = curses.color_pair(5) if avail >= 99 else curses.color_pair(4)
        safe_addstr(stdscr, y, rx + 20, f"Avail:{avail:.1f}%", c2)
        y += 1

    y += 1
    safe_addstr(stdscr, y, rx, "NOTICE", curses.A_BOLD)
    y += 1
    for app in APPS:
        total = data[app].get("total", 0)
        s5 = data[app].get("5xx", 0)
        p95 = calc_percentile(lats[app], 95)
        target = SLO[app]["target_ms"]
        if total > 10 and s5 / total > 0.05:
            safe_addstr(stdscr, y, rx, f"[!] {app}: 5xx high → scale-out", curses.color_pair(4))
        elif p95 > target * 2:
            safe_addstr(stdscr, y, rx, f"[!] {app}: p95={p95:.0f}ms → scale-out", curses.color_pair(4))
        elif p95 > target:
            safe_addstr(stdscr, y, rx, f"[~] {app}: p95 approaching SLO", curses.color_pair(2))
        elif total > 0:
            safe_addstr(stdscr, y, rx, f"[OK] {app}: normal", curses.color_pair(5))
        else:
            safe_addstr(stdscr, y, rx, f"[..] {app}: waiting", curses.color_pair(6))
        y += 1


def draw_logs(stdscr, buffers, lock, height, width, C, focus=-1, scroll=0, paused=False):
    apps = [APPS[focus]] if focus >= 0 else APPS
    panel_w = width // len(apps)
    for i, app in enumerate(apps):
        x = i * panel_w
        label = f" {app.upper()} "
        if paused:
            label += "[PAUSED] "
        safe_addstr(stdscr, 1, x + (panel_w - len(label)) // 2, label, curses.color_pair(C[app]) | curses.A_BOLD)
        with lock:
            lines = list(buffers[app])
        avail = height - 3
        end = len(lines) - scroll
        start = max(0, end - avail)
        end = max(start, end)
        visible = lines[start:end]
        for j, line in enumerate(visible):
            display = line[:panel_w - 2]
            y = j + 2
            if y >= height:
                break
            if "500" in line or '"5' in line:
                safe_addstr(stdscr, y, x, display, curses.color_pair(4))
            elif "40" in line and ('"4' in line or "| 4" in line):
                safe_addstr(stdscr, y, x, display, curses.color_pair(2))
            elif "200" in line or "201" in line or '"2' in line:
                safe_addstr(stdscr, y, x, display, curses.color_pair(5))
            else:
                safe_addstr(stdscr, y, x, display)
        if i < len(apps) - 1:
            for y in range(1, height):
                safe_addstr(stdscr, y, (i + 1) * panel_w - 1, "|")


def draw_raw(stdscr, buffers, lock, height, width, C, focus=-1, scroll=0, paused=False):
    apps = [APPS[focus]] if focus >= 0 else APPS
    panel_w = width // len(apps)
    for i, app in enumerate(apps):
        x = i * panel_w
        label = f" {app.upper()} [RAW] "
        if paused:
            label += "[PAUSED] "
        safe_addstr(stdscr, 1, x + (panel_w - len(label)) // 2, label, curses.color_pair(C[app]) | curses.A_BOLD)
        with lock:
            lines = list(buffers.get(f"{app}_raw", []))
        avail = height - 3
        end = len(lines) - scroll
        start = max(0, end - avail)
        end = max(start, end)
        visible = lines[start:end]
        for j, line in enumerate(visible):
            safe_addstr(stdscr, j + 2, x, line[:panel_w - 2])
        if i < len(apps) - 1:
            for y in range(1, height):
                safe_addstr(stdscr, y, (i + 1) * panel_w - 1, "|")
        if i < len(APPS) - 1:
            for y in range(1, height):
                safe_addstr(stdscr, y, (i + 1) * panel_w - 1, "|")


def draw_pods(stdscr, height, width, C):
    """Tab 4: Pod + HPA 상태 조회 (캐시 사용)"""
    global _pods_cache
    if "_pods_cache" not in globals():
        _pods_cache = {"pods": "", "hpa": "", "nodes": "", "top": "", "ts": 0}

    # 5초마다 백그라운드 갱신
    now = time.time()
    if now - _pods_cache["ts"] > 5:
        _pods_cache["ts"] = now
        def _fetch():
            def kr(args):
                try:
                    return subprocess.run(args, capture_output=True, text=True, timeout=3).stdout.strip()
                except:
                    return ""
            _pods_cache["pods"] = kr(["kubectl", "get", "pods", "-n", NAMESPACE, "--no-headers"])
            _pods_cache["hpa"] = kr(["kubectl", "get", "hpa", "-n", NAMESPACE, "--no-headers"])
            _pods_cache["nodes"] = kr(["kubectl", "get", "nodes", "--no-headers"])
            _pods_cache["top"] = kr(["kubectl", "top", "pods", "-n", NAMESPACE, "--no-headers"])
        threading.Thread(target=_fetch, daemon=True).start()

    pod_result = _pods_cache["pods"]
    hpa_result = _pods_cache["hpa"]
    node_result = _pods_cache["nodes"]
    top_result = _pods_cache["top"]

    mid_x = width // 2
    # Left: Pods
    y = 2
    safe_addstr(stdscr, y, 1, "PODS", curses.A_BOLD)
    y += 1
    safe_addstr(stdscr, y, 1, f"{'Name':<40}{'Status':<10}{'Restarts':<10}{'App':<8}")
    y += 1
    safe_addstr(stdscr, y, 1, "-" * (mid_x - 2))
    y += 1

    for line in pod_result.split("\n"):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) >= 3:
            name = parts[0][:38]
            status = parts[2] if len(parts) > 2 else "?"
            restarts = parts[3] if len(parts) > 3 else "0"
            app = "user" if "user" in name else "product" if "product" in name else "stress" if "stress" in name else "?"
            color = curses.color_pair(C.get(app, 6))
            sc = curses.color_pair(5) if "Running" in status else curses.color_pair(4)
            safe_addstr(stdscr, y, 1, f"{name:<40}", color)
            safe_addstr(stdscr, y, 41, f"{status:<10}", sc)
            safe_addstr(stdscr, y, 51, f"{restarts:<10}")
            safe_addstr(stdscr, y, 61, f"{app:<8}", color)
            y += 1
            if y >= height - 1:
                break

    # Right top: HPA
    y = 2
    rx = mid_x + 1
    safe_addstr(stdscr, y, rx, "HPA", curses.A_BOLD)
    y += 1
    safe_addstr(stdscr, y, rx, f"{'Name':<16}{'Ref':<10}{'Min':>4}{'Max':>4}{'Cur':>4}{'CPU%':>6}")
    y += 1
    safe_addstr(stdscr, y, rx, "-" * (width - mid_x - 3))
    y += 1

    for line in hpa_result.split("\n"):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) >= 6:
            name = parts[0][:15]
            ref = parts[1][:9] if len(parts) > 1 else "?"
            targets = parts[2] if len(parts) > 2 else "?"
            minp = parts[3] if len(parts) > 3 else "?"
            maxp = parts[4] if len(parts) > 4 else "?"
            cur = parts[5] if len(parts) > 5 else "?"
            safe_addstr(stdscr, y, rx, f"{name:<16}{ref:<10}{minp:>4}{maxp:>4}{cur:>4}  {targets}")
            y += 1

    # Right bottom: Nodes
    y += 2
    safe_addstr(stdscr, y, rx, "NODES", curses.A_BOLD)
    y += 1
    for line in node_result.split("\n"):
        if not line.strip():
            continue
        safe_addstr(stdscr, y, rx, line[:width - mid_x - 3])
        y += 1

    # Right bottom: Top pods
    y += 2
    safe_addstr(stdscr, y, rx, "TOP PODS (CPU/MEM)", curses.A_BOLD)
    y += 1
    for line in top_result.split("\n")[:8]:
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) >= 3:
            name = parts[0][:30]
            cpu = parts[1]
            mem = parts[2]
            app = "user" if "user" in name else "product" if "product" in name else "stress" if "stress" in name else ""
            color = curses.color_pair(C.get(app, 6))
            safe_addstr(stdscr, y, rx, f"{name:<32}{cpu:>8}{mem:>10}", color)
            y += 1


def main(stdscr):
    pods = get_pods()
    if not pods:
        stdscr.addstr(0, 0, "No pods found. Press any key.")
        stdscr.getch()
        return

    lock = threading.Lock()
    buffers = {app: deque(maxlen=MAX_LINES) for app in APPS}
    for app in APPS:
        buffers[f"{app}_raw"] = deque(maxlen=MAX_LINES)
    stats = {app: {
        "total": 0, "2xx": 0, "4xx": 0, "5xx": 0,
        "slo_breach": 0,
        "latencies": deque(maxlen=1000),
        "paths": defaultdict(lambda: {"total": 0, "2xx": 0, "4xx": 0, "5xx": 0}),
    } for app in APPS}

    for name, app in pods:
        if app in APPS:
            t = threading.Thread(target=stream_to_buffer, args=(name, app, buffers, stats, lock), daemon=True)
            t.start()

    draw(stdscr, buffers, stats, lock)


if __name__ == "__main__":
    curses.wrapper(main)
