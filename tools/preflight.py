"""
preflight.py
인프라 + API + WAF 전체 헬스체크 (채점 시작 전 1회).

검사 항목:
  1. EKS 노드 Ready / 워커 수
  2. user/product/stress Deployment AVAILABLE >= 1
  3. HPA TARGETS 메트릭 정상 (<unknown> 아님 → metrics-server 동작)
  4. CloudFront 경유 정상 요청(GET user/product, POST stress) 2xx
  5. WAF 비정상 요청(잘못된 uuid) 차단(403) — default-block 동작 확인

사용법: python preflight.py <CloudFront endpoint>
종료코드: 0=전부 통과, 1=실패 항목 존재
의존성: 표준 라이브러리만 (kubectl 은 PATH 에 있어야 함)
"""
import json
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

NAMESPACE = "apdev"
APPS = ["user", "product", "stress"]

_OK, _WARN, _FAIL = "\033[92m✓\033[0m", "\033[93m!\033[0m", "\033[91m✗\033[0m"
_results = []


def record(ok, label, detail=""):
    mark = _OK if ok is True else (_WARN if ok is None else _FAIL)
    print(f"  {mark} {label:<34} {detail}")
    _results.append(ok is not False)  # None(warn) 은 실패로 치지 않음


def kubectl(cmd):
    r = subprocess.run(f"kubectl {cmd}", shell=True, capture_output=True, text=True)
    return r.returncode == 0, r.stdout.strip()


def rid():
    return str(random.randint(100000000000, 999999999999))


def uid():
    return str(uuid.uuid4())


def http(method, url, body=None, timeout=8):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, (time.time() - t0) * 1000
    except Exception:
        return 0, (time.time() - t0) * 1000


def check_nodes():
    ok, out = kubectl("get nodes --no-headers")
    if not ok or not out:
        record(False, "EKS 노드", "kubectl 접근 실패")
        return
    lines = [l for l in out.splitlines() if l.strip()]
    ready = [l for l in lines if " Ready" in l]
    record(len(ready) >= 1, "EKS 노드 Ready", f"{len(ready)}/{len(lines)} ready")


def check_deployments():
    for app in APPS:
        ok, out = kubectl(f"-n {NAMESPACE} get deploy/{app} --no-headers "
                          f"-o custom-columns=A:.status.availableReplicas")
        try:
            avail = int(out.strip())
        except Exception:
            avail = 0
        record(avail >= 1, f"Deployment {app}", f"available={avail}")


def check_hpa():
    for app in APPS:
        ok, out = kubectl(f"-n {NAMESPACE} get hpa/{app}-hpa --no-headers "
                          f"-o custom-columns=T:.status.currentMetrics")
        ok2, tgt = kubectl(f"-n {NAMESPACE} get hpa/{app}-hpa --no-headers")
        unknown = (not ok) or ("unknown" in (out or "").lower()) or ("<unknown>" in (tgt or ""))
        record(None if unknown else True,
               f"HPA {app} 메트릭",
               "metrics-server 미준비(<unknown>)" if unknown else "정상")


def check_apis(base):
    seed = f"_pf_{random.randint(1000000, 9999999)}"
    cases = [
        ("user", "GET", f"{base}/v1/user?email={seed}@t.org&requestid={rid()}&uuid={uid()}", None),
        ("product", "GET", f"{base}/v1/product?id={seed}&requestid={rid()}&uuid={uid()}", None),
        ("stress", "POST", f"{base}/v1/stress", {"requestid": rid(), "uuid": uid(), "length": 256}),
    ]
    for app, method, url, body in cases:
        status, ms = http(method, url, body)
        # 도달성 기준: 연결됨(≠0), 서버오류 아님(<500), WAF 오차단 아님(≠403)
        ok = status != 0 and status < 500 and status != 403
        record(ok, f"API {app} ({method})", f"status={status} {ms:.0f}ms")


def check_waf_block(base):
    # 잘못된 uuid → 화이트리스트 불일치 → default block(403) 이어야 함
    bad = f"{base}/v1/user?email=x@t.org&requestid={rid()}&uuid=not-a-uuid"
    status, ms = http("GET", bad)
    record(status == 403, "WAF 비정상요청 차단", f"status={status} (기대 403)")


def main():
    if len(sys.argv) < 2:
        print("사용법: python preflight.py <CloudFront endpoint>")
        sys.exit(2)
    base = sys.argv[1].rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base

    print(f"\n=== Preflight: {base} ===\n")
    print(" [인프라]")
    check_nodes()
    check_deployments()
    check_hpa()
    print("\n [API / WAF]")
    check_apis(base)
    check_waf_block(base)

    passed = all(_results)
    print(f"\n{'='*48}")
    print(f"  {_OK + ' 전체 통과' if passed else _FAIL + ' 실패 항목 존재 — 위 항목 점검 후 재실행'}")
    print(f"{'='*48}\n")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
