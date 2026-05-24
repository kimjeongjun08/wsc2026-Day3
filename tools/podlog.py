#!/usr/bin/env python3
import subprocess
import threading
import sys
import json
import os
import time
from datetime import datetime

NAMESPACE = "apdev"
COLORS = {"user": "\033[96m", "product": "\033[93m", "stress": "\033[95m"}
RED = "\033[91m"
GREEN = "\033[92m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

stats = {"total": 0, "errors": 0, "by_app": {}}
lock = threading.Lock()
log_2xx = []
log_4xx = []
log_5xx = []
log_other = []
LOG_DIR = "logs"


def get_pods():
    result = subprocess.run(
        ["kubectl", "get", "pods", "-n", NAMESPACE, "-o", "custom-columns=NAME:.metadata.name,APP:.metadata.labels.app", "--no-headers"],
        capture_output=True, text=True
    )
    pods = []
    for line in result.stdout.strip().split("\n"):
        if line.strip():
            parts = line.split()
            if len(parts) >= 2:
                pods.append((parts[0], parts[1]))
    return pods


def parse_log_line(line):
    # 앱 로그 형식: {"ts":"...","msg":"...","status":200,"method":"GET","path":"/v1/user",...}
    try:
        data = json.loads(line)
        return data
    except:
        return None


def format_line(app, pod_short, line):
    if "/healthcheck" in line:
        return None

    color = COLORS.get(app, "")
    now = datetime.now().strftime("%H:%M:%S")
    data = parse_log_line(line)

    with lock:
        stats["total"] += 1
        stats["by_app"][app] = stats["by_app"].get(app, 0) + 1

    if data:
        method = data.get("method", "?")
        path = data.get("path", data.get("msg", ""))
        status = data.get("status", data.get("code", ""))
        latency = data.get("latency", data.get("elapsed", ""))
        msg = data.get("msg", "")

        # 상태코드 색상
        if isinstance(status, int):
            if status >= 500:
                sc = f"{RED}{status}{RESET}"
                with lock:
                    stats["errors"] += 1
            elif status >= 400:
                sc = f"{RED}{status}{RESET}"
                with lock:
                    stats["errors"] += 1
            elif status >= 200:
                sc = f"{GREEN}{status}{RESET}"
            else:
                sc = str(status)
        else:
            sc = str(status)

        # 레이턴시 포맷
        if isinstance(latency, (int, float)):
            if latency > 1000:
                lat = f"{RED}{latency:.0f}ms{RESET}"
            elif latency > 200:
                lat = f"\033[93m{latency:.0f}ms{RESET}"
            else:
                lat = f"{DIM}{latency:.0f}ms{RESET}"
        elif latency:
            lat = str(latency)
        else:
            lat = ""

        if method and path and status:
            return f"{DIM}{now}{RESET} {color}▌{app:<7}{RESET} {BOLD}{method:<4}{RESET} {path:<25} {sc}  {lat}"
        elif msg:
            if "error" in msg.lower() or "fail" in msg.lower():
                with lock:
                    stats["errors"] += 1
                return f"{DIM}{now}{RESET} {color}▌{app:<7}{RESET} {RED}✗ {msg}{RESET}"
            elif "listening" in msg.lower() or "started" in msg.lower():
                return f"{DIM}{now}{RESET} {color}▌{app:<7}{RESET} {GREEN}● {msg}{RESET}"
            else:
                return f"{DIM}{now}{RESET} {color}▌{app:<7}{RESET}   {msg}"
    else:
        # plain text log
        if "error" in line.lower() or "fail" in line.lower() or "panic" in line.lower():
            with lock:
                stats["errors"] += 1
            return f"{DIM}{now}{RESET} {color}▌{app:<7}{RESET} {RED}✗ {line}{RESET}"
        elif "waiting for db" in line.lower():
            return f"{DIM}{now}{RESET} {color}▌{app:<7}{RESET} {DIM}⏳ {line}{RESET}"
        else:
            return f"{DIM}{now}{RESET} {color}▌{app:<7}{RESET}   {line}"


def stream(pod_name, app):
    pod_short = pod_name[-5:]
    proc = subprocess.Popen(
        ["kubectl", "logs", "-f", "--tail", "100", pod_name, "-n", NAMESPACE],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        output = format_line(app, pod_short, line)
        if output:
            entry = f"{datetime.now().isoformat()} [{app}] {line}\n"
            data = parse_log_line(line)
            status = data.get("status", 0) if data else 0
            with lock:
                if isinstance(status, int) and status >= 500:
                    log_5xx.append(entry)
                elif isinstance(status, int) and status >= 400:
                    log_4xx.append(entry)
                elif isinstance(status, int) and status >= 200:
                    log_2xx.append(entry)
                else:
                    log_other.append(entry)
            print(output)


def print_header(pod_count):
    print(f"\n{BOLD}╔══════════════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}║  Pod Log Monitor │ ns: {NAMESPACE:<8} │ pods: {pod_count:<3}              ║{RESET}")
    print(f"{BOLD}╠══════════════════════════════════════════════════════════════╣{RESET}")
    print(f"{BOLD}║{RESET}  {COLORS['user']}■ user{RESET}    {COLORS['product']}■ product{RESET}    {COLORS['stress']}■ stress{RESET}               {BOLD}║{RESET}")
    print(f"{BOLD}╚══════════════════════════════════════════════════════════════╝{RESET}")
    print(f"{DIM}  healthcheck 제외 │ Ctrl+C 종료{RESET}\n")


def flush_logs():
    while True:
        time.sleep(60)
        with lock:
            if not log_2xx and not log_4xx and not log_5xx and not log_other:
                continue
            os.makedirs(LOG_DIR, exist_ok=True)
            filename = os.path.join(LOG_DIR, datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
            with open(filename, "w", encoding="utf-8") as f:
                if log_5xx:
                    f.write(f"{'='*60}\n")
                    f.write(f"  [5xx SERVER ERROR] - {len(log_5xx)} entries\n")
                    f.write(f"{'='*60}\n")
                    f.writelines(log_5xx)
                    f.write("\n")
                if log_4xx:
                    f.write(f"{'='*60}\n")
                    f.write(f"  [4xx CLIENT ERROR] - {len(log_4xx)} entries\n")
                    f.write(f"{'='*60}\n")
                    f.writelines(log_4xx)
                    f.write("\n")
                if log_2xx:
                    f.write(f"{'='*60}\n")
                    f.write(f"  [2xx SUCCESS] - {len(log_2xx)} entries\n")
                    f.write(f"{'='*60}\n")
                    f.writelines(log_2xx)
                    f.write("\n")
                if log_other:
                    f.write(f"{'='*60}\n")
                    f.write(f"  [OTHER] - {len(log_other)} entries\n")
                    f.write(f"{'='*60}\n")
                    f.writelines(log_other)
            total = len(log_5xx) + len(log_4xx) + len(log_2xx) + len(log_other)
            log_5xx.clear()
            log_4xx.clear()
            log_2xx.clear()
            log_other.clear()
        print(f"{DIM}  💾 {total} lines → {filename}{RESET}")


def main():
    pods = get_pods()
    if not pods:
        print(f"{RED}No pods found in namespace [{NAMESPACE}]{RESET}")
        sys.exit(1)

    print_header(len(pods))

    threading.Thread(target=flush_logs, daemon=True).start()

    threads = []
    for name, app in pods:
        t = threading.Thread(target=stream, args=(name, app), daemon=True)
        t.start()
        threads.append(t)

    try:
        while True:
            for t in threads:
                t.join(timeout=1)
    except KeyboardInterrupt:
        print(f"\n{DIM}{'─'*60}{RESET}")
        print(f"{BOLD}  Summary{RESET}: {stats['total']} requests │ {RED}{stats['errors']} errors{RESET} │ {stats['by_app']}")
        print()


if __name__ == "__main__":
    main()
