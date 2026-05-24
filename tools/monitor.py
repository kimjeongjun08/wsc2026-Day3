#!/usr/bin/env python3
"""
onda-monitor: 애플리케이션 엔드포인트 모니터링

  python onda-monitor.py                    # 계속 모니터링 (Ctrl+C 종료)
  python onda-monitor.py -i 10              # 10초 간격
  python onda-monitor.py --once             # 1회만 체크
  python onda-monitor.py --add GET /api/v1/products  # 엔드포인트 추가
  python onda-monitor.py --remove /healthz  # 엔드포인트 제거
  python onda-monitor.py --list             # 등록된 엔드포인트 목록
"""

import argparse, json, time, sys, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
CONF = Path(__file__).parent / ".monitor-endpoints.json"

DEFAULT_ENDPOINTS = [
    {"method": "GET",  "path": "/healthz",          "expect": 200, "desc": "헬스체크"},
    {"method": "GET",  "path": "/",                  "expect": 200, "desc": "메인"},
    {"method": "GET",  "path": "/api/v1/products",   "expect": 200, "desc": "상품 목록"},
    {"method": "GET",  "path": "/api/v1/products/1", "expect": 200, "desc": "상품 상세"},
    {"method": "POST", "path": "/api/v1/orders",     "expect": [200,201,401], "desc": "주문 생성"},
    {"method": "GET",  "path": "/api/v1/orders",     "expect": [200,401], "desc": "주문 목록"},
    {"method": "POST", "path": "/api/v1/auth/login", "expect": [200,400,401], "desc": "로그인"},
    {"method": "GET",  "path": "/api/v1/users/me",   "expect": [200,401], "desc": "내 정보"},
    {"method": "GET",  "path": "/nonexistent",       "expect": 404, "desc": "404 확인"},
]

def load_endpoints():
    if CONF.exists():
        return json.loads(CONF.read_text())
    save_endpoints(DEFAULT_ENDPOINTS)
    return DEFAULT_ENDPOINTS

def save_endpoints(eps):
    CONF.write_text(json.dumps(eps, ensure_ascii=False, indent=2))

def get_base_url():
    """ALB DNS 가져오기"""
    try:
        import boto3
        elb = boto3.client("elbv2", region_name="ap-northeast-2")
        lbs = elb.describe_load_balancers(Names=["onda-mart-alb"])["LoadBalancers"]
        if lbs:
            return f"http://{lbs[0]['DNSName']}"
    except:
        pass
    return None

def check(base, ep):
    """단일 엔드포인트 체크"""
    url = base + ep["path"]
    method = ep["method"]
    expects = ep["expect"] if isinstance(ep["expect"], list) else [ep["expect"]]

    start = time.time()
    try:
        req = urllib.request.Request(url, method=method)
        req.add_header("Content-Type", "application/json")
        if method == "POST":
            req.data = b"{}"
        resp = urllib.request.urlopen(req, timeout=10)
        status = resp.status
        body = resp.read().decode("utf-8", errors="replace")[:200]
        latency = (time.time() - start) * 1000
    except urllib.error.HTTPError as e:
        status = e.code
        body = e.read().decode("utf-8", errors="replace")[:200]
        latency = (time.time() - start) * 1000
    except Exception as e:
        return {"status": 0, "body": str(e)[:100], "latency": -1, "ok": False}

    ok = status in expects
    return {"status": status, "body": body.strip().replace("\n"," ")[:80], "latency": latency, "ok": ok}

def bar_lat(ms, w=10):
    if ms < 0: return "  FAIL  "
    cap = min(ms, 2000)
    filled = int(cap / 2000 * w)
    return "█" * filled + "░" * (w - filled)

def run_check(base, endpoints):
    now = datetime.now(KST).strftime("%H:%M:%S")
    print(f"\n[{now}] 엔드포인트 체크 — {base}")
    print(f"{'':>4} {'메서드':>6} {'경로':<25} {'상태':>4} {'예상':>8} {'판정':>4} {'지연':>7} {'그래프':<12} {'응답'}")
    print("─" * 120)

    results = {"total": 0, "ok": 0, "fail": 0, "errors": []}
    for i, ep in enumerate(endpoints, 1):
        r = check(base, ep)
        results["total"] += 1
        expects = ep["expect"] if isinstance(ep["expect"], list) else [ep["expect"]]
        exp_str = ",".join(str(e) for e in expects)

        if r["ok"]:
            results["ok"] += 1
            mark = "✅"
        else:
            results["fail"] += 1
            mark = "❌"
            results["errors"].append(f"{ep['method']} {ep['path']} → {r['status']}")

        lat = f"{r['latency']:.0f}ms" if r["latency"] >= 0 else "FAIL"
        body = r["body"][:50] if r["body"] else ""
        print(f"{i:>3}. {ep['method']:>6} {ep['path']:<25} {r['status']:>4} {exp_str:>8} {mark:>4} {lat:>7} {bar_lat(r['latency']):<12} {body}")

    print(f"─" * 120)
    pct = results["ok"] / results["total"] * 100 if results["total"] else 0
    print(f"  결과: {results['ok']}/{results['total']} 통과 ({pct:.0f}%)", end="")
    if results["fail"]:
        print(f"  ❌ 실패: {', '.join(results['errors'])}")
    else:
        print()
    return results

def main():
    p = argparse.ArgumentParser(description="onda-monitor: 엔드포인트 모니터링")
    p.add_argument("-i", "--interval", type=int, default=5, help="체크 간격 초 (기본 5)")
    p.add_argument("--once", action="store_true", help="1회만 실행")
    p.add_argument("--url", help="베이스 URL (기본: ALB DNS 자동)")
    p.add_argument("--add", nargs=2, metavar=("METHOD", "PATH"), help="엔드포인트 추가")
    p.add_argument("--remove", metavar="PATH", help="엔드포인트 제거")
    p.add_argument("--list", action="store_true", help="등록된 엔드포인트 목록")
    a = p.parse_args()

    eps = load_endpoints()

    if a.list:
        print(f"\n등록된 엔드포인트 ({len(eps)}개):\n")
        for i, ep in enumerate(eps, 1):
            expects = ep["expect"] if isinstance(ep["expect"], list) else [ep["expect"]]
            print(f"  {i}. {ep['method']:>6} {ep['path']:<30} expect={expects}  {ep.get('desc','')}")
        return

    if a.add:
        method, path = a.add[0].upper(), a.add[1]
        eps.append({"method": method, "path": path, "expect": 200, "desc": ""})
        save_endpoints(eps)
        print(f"✅ 추가: {method} {path}")
        return

    if a.remove:
        eps = [e for e in eps if e["path"] != a.remove]
        save_endpoints(eps)
        print(f"✅ 제거: {a.remove}")
        return

    base = a.url or get_base_url()
    if not base:
        print("ALB를 찾을 수 없습니다. --url http://... 으로 직접 지정하세요.")
        return

    if a.once:
        run_check(base, eps)
    else:
        print(f"🔄 모니터링 시작 (간격={a.interval}초, Ctrl+C 종료)")
        while True:
            try:
                run_check(base, eps)
                time.sleep(a.interval)
            except KeyboardInterrupt:
                print("\n중지."); break

if __name__ == "__main__":
    main()
