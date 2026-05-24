#!/usr/bin/env python3
"""
CloudFront 로그 모니터링 (CloudWatch cflog 로그 그룹 대상)
비정상 트래픽 탐지 + 실시간 시각화
"""
import subprocess
import json
import sys
import time
import os
import threading
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
os.environ["PYTHONIOENCODING"] = "utf-8"

REGION = "ap-northeast-2"
LOG_GROUP = "cflogs"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
RESET = "\033[0m"

LOG_DIR = "logs"
lock = threading.Lock()
log_normal = []
log_suspicious = []

# 비정상 UA 패턴
BAD_UA = [
    "nikto", "sqlmap", "nmap", "masscan", "dirbuster", "gobuster", "wfuzz",
    "hydra", "burp", "nessus", "acunetix", "w3af", "arachni", "zgrab",
    "nuclei", "httpx", "feroxbuster", "whatweb", "wpscan", "striker",
    "bot", "crawler", "spider", "scan", "exploit", "attack", "hack",
    "python-requests", "go-http-client", "java/", "wget", "libwww"
]

# 비정상 경로 패턴
BAD_PATH = [
    ".env", ".git", ".aws", "wp-admin", "wp-login", "phpmyadmin",
    "/admin", "/actuator", "/debug", "/console", "/../", "/etc/passwd",
    "/proc/", ".php", ".asp", "cgi-bin"
]

stats = {"total": 0, "normal": 0, "suspicious": 0, "blocked_ua": {}, "blocked_path": {}}


def is_suspicious(entry):
    reasons = []
    ua = entry.get("ua", "").lower()
    path = entry.get("path", "").lower()
    status = entry.get("status", 0)
    headers = entry.get("headers", "").lower()

    for bad in BAD_UA:
        if bad in ua or bad in headers:
            reasons.append(f"UA:{bad}")
            stats["blocked_ua"][bad] = stats["blocked_ua"].get(bad, 0) + 1
            break

    for bad in BAD_PATH:
        if bad in path:
            reasons.append(f"PATH:{bad}")
            stats["blocked_path"][bad] = stats["blocked_path"].get(bad, 0) + 1
            break

    if isinstance(status, int) and status == 403:
        reasons.append("WAF_BLOCKED")

    return reasons


def parse_cf_log(message):
    try:
        data = json.loads(message)
        return data
    except:
        pass

    # space-separated CloudFront log format
    parts = message.split("\t") if "\t" in message else message.split(" ")
    if len(parts) >= 12:
        try:
            return {
                "timestamp": parts[0] + " " + parts[1] if len(parts[0]) == 10 else parts[0],
                "client": parts[4] if len(parts) > 4 else "?",
                "method": parts[5] if len(parts) > 5 else "?",
                "path": parts[7] if len(parts) > 7 else "?",
                "status": int(parts[8]) if len(parts) > 8 and parts[8].isdigit() else 0,
                "ua": parts[10] if len(parts) > 10 else "",
                "latency": parts[18] if len(parts) > 18 else "",
                "headers": " ".join(parts[10:]) if len(parts) > 10 else "",
            }
        except:
            pass
    return {"raw": message, "status": 0, "ua": "", "path": "", "headers": ""}


def format_entry(entry, reasons):
    path = entry.get("path", entry.get("raw", "?"))[:40]
    status = entry.get("status", "?")
    method = entry.get("method", "?")
    client = entry.get("client", "?")[:15]
    ua = entry.get("ua", "")[:30]
    latency = entry.get("latency", "")

    # 상태코드 색상
    try:
        code = int(status)
        if code >= 500:
            sc = f"{RED}{status}{RESET}"
        elif code >= 400:
            sc = f"{YELLOW}{status}{RESET}"
        else:
            sc = f"{GREEN}{status}{RESET}"
    except:
        sc = str(status)

    now = datetime.now().strftime("%H:%M:%S")

    if reasons:
        flag = f"{RED}⚠ {','.join(reasons)}{RESET}"
        return f"{DIM}{now}{RESET} {RED}▌{RESET}{client:<15} {BOLD}{method:<5}{RESET}{path:<40} {sc} {flag} {DIM}{ua}{RESET}"
    else:
        return f"{DIM}{now}{RESET} {GREEN}▌{RESET}{client:<15} {BOLD}{method:<5}{RESET}{path:<40} {sc} {DIM}{ua}{RESET}"


def flush_logs():
    while True:
        time.sleep(60)
        with lock:
            if not log_normal and not log_suspicious:
                continue
            os.makedirs(LOG_DIR, exist_ok=True)
            filename = os.path.join(LOG_DIR, "cf_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
            with open(filename, "w", encoding="utf-8") as f:
                if log_suspicious:
                    f.write(f"{'='*70}\n")
                    f.write(f"  ⚠ SUSPICIOUS TRAFFIC - {len(log_suspicious)} entries\n")
                    f.write(f"{'='*70}\n")
                    f.writelines(log_suspicious)
                    f.write("\n")
                if log_normal:
                    f.write(f"{'='*70}\n")
                    f.write(f"  ✓ NORMAL TRAFFIC - {len(log_normal)} entries\n")
                    f.write(f"{'='*70}\n")
                    f.writelines(log_normal)
                # 요약
                f.write(f"\n{'='*70}\n")
                f.write(f"  SUMMARY: total={stats['total']} normal={stats['normal']} suspicious={stats['suspicious']}\n")
                if stats["blocked_ua"]:
                    f.write(f"  Blocked UA: {dict(sorted(stats['blocked_ua'].items(), key=lambda x:-x[1])[:10])}\n")
                if stats["blocked_path"]:
                    f.write(f"  Blocked Path: {dict(sorted(stats['blocked_path'].items(), key=lambda x:-x[1])[:10])}\n")
                f.write(f"{'='*70}\n")
            total = len(log_suspicious) + len(log_normal)
            log_suspicious.clear()
            log_normal.clear()
        print(f"{DIM}  💾 {total} lines → {filename}{RESET}")


def tail_logs():
    # CloudWatch Logs tail
    cmd = [
        "aws", "logs", "tail", LOG_GROUP,
        "--follow", "--format", "short",
        "--region", REGION
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    for line in proc.stdout:
        line = line.rstrip()
        if not line or "/healthcheck" in line:
            continue

        entry = parse_cf_log(line)
        reasons = is_suspicious(entry)

        stats["total"] += 1
        output = format_entry(entry, reasons)

        raw_line = f"{datetime.now().isoformat()} {line}\n"
        with lock:
            if reasons:
                stats["suspicious"] += 1
                log_suspicious.append(raw_line)
            else:
                stats["normal"] += 1
                log_normal.append(raw_line)

        if output:
            print(output)


def main():
    print(f"\n{BOLD}╔══════════════════════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}║  CF Log Monitor │ group: {LOG_GROUP:<10} │ region: {REGION}  ║{RESET}")
    print(f"{BOLD}╠══════════════════════════════════════════════════════════════════════╣{RESET}")
    print(f"{BOLD}║{RESET}  {GREEN}▌ normal{RESET}    {RED}▌⚠ suspicious{RESET}    healthcheck 제외          {BOLD}║{RESET}")
    print(f"{BOLD}╚══════════════════════════════════════════════════════════════════════╝{RESET}")
    print(f"{DIM}  Ctrl+C 종료 │ 1분마다 로그 저장{RESET}\n")

    threading.Thread(target=flush_logs, daemon=True).start()

    try:
        tail_logs()
    except KeyboardInterrupt:
        print(f"\n{DIM}{'─'*70}{RESET}")
        print(f"{BOLD}  Summary{RESET}: {stats['total']} total │ {GREEN}{stats['normal']} normal{RESET} │ {RED}{stats['suspicious']} suspicious{RESET}")
        if stats["blocked_ua"]:
            top_ua = sorted(stats["blocked_ua"].items(), key=lambda x: -x[1])[:5]
            print(f"  {RED}Top blocked UA{RESET}: {', '.join(f'{k}({v})' for k,v in top_ua)}")
        if stats["blocked_path"]:
            top_path = sorted(stats["blocked_path"].items(), key=lambda x: -x[1])[:5]
            print(f"  {RED}Top blocked Path{RESET}: {', '.join(f'{k}({v})' for k,v in top_path)}")
        print()


if __name__ == "__main__":
    main()
