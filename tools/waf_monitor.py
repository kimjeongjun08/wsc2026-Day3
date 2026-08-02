"""
waf_monitor.py — 바디 로깅 + stress length 추적

용도: WAF 로그에서 못 보는 request body를 파드 로그에서 확인
핵심: stress length MAX 실시간 추적 (과대 length 감지)

사용법: python waf_monitor.py [--interval 3] [--tail 80]
"""
import subprocess
import json
import sys
import time
import argparse
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def get_pod_logs(app: str, tail: int = 80) -> list:
    """kubectl logs에서 JSON 파싱 가능한 로그만 가져오기 (healthcheck 제외)"""
    try:
        cmd = ["kubectl", "logs", "-n", "apdev", "-l", f"app={app}", "--tail", str(tail)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return []
        lines = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                log = json.loads(line)
                if log.get("path") == "/healthcheck":
                    continue
                lines.append(log)
            except json.JSONDecodeError:
                continue
        return lines
    except Exception:
        return []


def print_dashboard(all_logs: dict, stress_stats: dict):
    """바디 모니터 — stress length + user/product 요청 바디"""
    print("\033[2J\033[H")

    max_len = stress_stats["max_length"]
    recent = stress_stats["recent_lengths"]
    total = stress_stats["total"]
    over_1k = stress_stats["over_1000"]
    over_10k = stress_stats["over_10000"]

    # stress length 색상
    if max_len >= 100000:
        c = "\033[31;1m"
    elif max_len >= 10000:
        c = "\033[31m"
    elif max_len >= 1000:
        c = "\033[33m"
    else:
        c = "\033[32m"
    rst = "\033[0m"

    print("╔══════════════════════════════════════════════════════════════════════════════════╗")
    print(f"║  바디 모니터 (stress length + user/product)                    {datetime.now().strftime('%H:%M:%S')}  ║")
    print("╠══════════════════════════════════════════════════════════════════════════════════╣")
    print(f"║  STRESS MAX LENGTH: {c}{max_len:>10}{rst}  │  총:{total}  ≥1k:{over_1k}  ≥10k:{over_10k}  │  최근: {' '.join(str(l) for l in recent[-6:])}")
    if max_len >= 100000:
        print(f"║  {c}⚠ 과대 length 감지! WAF 차단 필요!{rst}")
    print("╠══════════════════════════════════════════════════════════════════════════════════╣")

    # 각 앱별 최근 바디 정보
    for app in ["user", "product", "stress"]:
        logs = all_logs.get(app, [])
        if not logs:
            continue

        print(f"║  ── {app} {'─' * 70}║")

        reqs = [l for l in logs if l.get("path") != "/healthcheck"][-10:]
        for log in reqs:
            ts = (log.get("ts") or "")
            if "T" in ts:
                ts = ts.split("T")[1][:8]
            status = log.get("status", "?")
            dur = log.get("dur_ms") or log.get("latency_ms") or 0
            method = log.get("method", "?")
            path = log.get("path", "")

            # 바디 정보 추출
            body_info = ""
            if app == "stress":
                length = log.get("length", "")
                iterations = log.get("iterations", "")
                if length:
                    body_info = f"length={length}"
                elif iterations:
                    body_info = f"iter={iterations}"
            elif app == "user":
                requestid = log.get("requestid", "")
                uuid_val = log.get("uuid", "")[:8] if log.get("uuid") else ""
                # path에서 email 추출
                if "email=" in path:
                    email = path.split("email=")[1].split("&")[0][:25]
                    body_info = f"email={email}"
                elif requestid:
                    body_info = f"rid={requestid[:12]}"
            elif app == "product":
                requestid = log.get("requestid", "")
                if "id=" in path:
                    pid = path.split("id=")[1].split("&")[0][:20]
                    body_info = f"id={pid}"
                elif requestid:
                    body_info = f"rid={requestid[:12]}"

            # 색상
            line = f"{ts} {status:>3} {dur:>6.0f}ms {method:<4} {body_info:<40}"
            if isinstance(status, int) and status >= 500:
                print(f"║  \033[31m{line}\033[0m  ║")
            elif isinstance(status, int) and status >= 400:
                print(f"║  \033[33m{line}\033[0m  ║")
            else:
                print(f"║  {line}  ║")

    print("╚══════════════════════════════════════════════════════════════════════════════════╝")
    print("  Ctrl+C 종료")


def main():
    parser = argparse.ArgumentParser(description="stress length 바디 모니터")
    parser.add_argument("--interval", type=int, default=3, help="갱신 간격(초)")
    parser.add_argument("--tail", type=int, default=80, help="가져올 로그 수")
    args = parser.parse_args()

    stress_stats = {
        "max_length": 0,
        "recent_lengths": [],
        "total": 0,
        "over_1000": 0,
        "over_10000": 0,
    }

    seen_ts = set()  # 중복 방지

    print("stress length 바디 모니터 시작...")

    try:
        while True:
            all_logs = {}
            for app in ["user", "product", "stress"]:
                all_logs[app] = get_pod_logs(app, tail=args.tail)

            # stress length 추적
            for log in all_logs.get("stress", []):
                if log.get("path") != "/v1/stress":
                    continue
                if log.get("method") != "POST":
                    continue

                ts = log.get("ts", "")
                if ts in seen_ts:
                    continue
                seen_ts.add(ts)
                if len(seen_ts) > 500:
                    seen_ts.clear()

                length = log.get("length")
                if length is None:
                    iterations = log.get("iterations")
                    if iterations:
                        length = iterations // 260
                if length is None:
                    continue

                try:
                    length = int(length)
                except (ValueError, TypeError):
                    continue

                stress_stats["total"] += 1
                if length > stress_stats["max_length"]:
                    stress_stats["max_length"] = length
                if length >= 1000:
                    stress_stats["over_1000"] += 1
                if length >= 10000:
                    stress_stats["over_10000"] += 1
                stress_stats["recent_lengths"].append(length)
                if len(stress_stats["recent_lengths"]) > 30:
                    stress_stats["recent_lengths"] = stress_stats["recent_lengths"][-30:]

            print_dashboard(all_logs, stress_stats)
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print(f"\n\n최종 결과:")
        print(f"  MAX LENGTH: {stress_stats['max_length']}")
        print(f"  총 요청: {stress_stats['total']}")
        print(f"  ≥1000: {stress_stats['over_1000']}")
        print(f"  ≥10000: {stress_stats['over_10000']}")
        print("종료.")


if __name__ == "__main__":
    main()
