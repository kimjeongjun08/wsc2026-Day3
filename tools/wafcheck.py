"""
wafcheck.py
WAF(CloudFront) 로그를 분석해 정상 트래픽이 잘못 차단(자기-403)되고 있지 않은지 감시.

default-BLOCK 화이트리스트 전략은 비정상요청(403) 점수엔 좋지만, 규칙이 조금이라도 빡세면
정상 요청까지 403 → 가용성(12) + 성능(12) 폭락. 이 툴은 CloudWatch 의 WAF 로그를 읽어
BLOCK 을 (규칙 / URI 경로)별로 집계하고, "제공 API 경로(/v1/user|product|stress)로의 BLOCK"
을 강조한다 — 이 값이 크면 정상 트래픽 오차단 의심 → update_waf 헤더룰 제거나 규칙 완화 판단.

판단 가이드:
  - Default_Action(기본 block) 으로 /v1/* 가 많이 잡힘  → 정상 요청의 uuid/param 형식 규칙이 빡셀 수 있음
  - BlockUnknownHeaders 로 /v1/* 가 잡힘             → 헤더 화이트리스트가 정상 헤더를 막는 중(자기-DoS) → update_waf 재실행/미실행 검토
  - 미제공 경로(/v1/none 등)가 BLOCK                 → 404 여야 하는데 403 (AllowNonApiPaths 규칙 확인)

사용법:
  python wafcheck.py            # 최근 5분 분석
  python wafcheck.py 15         # 최근 15분 분석
의존성: boto3, 표준 라이브러리. WAF 로그 그룹(us-east-1)이 있어야 함.
"""
import sys
import time
from collections import Counter

import boto3

REGION = "us-east-1"          # CloudFront scope WAF 로그는 us-east-1
LOG_GROUP = "aws-waf-logs-apdev"
API_PREFIXES = ("/v1/user", "/v1/product", "/v1/stress")


def path_class(uri):
    if uri.startswith(API_PREFIXES):
        return "제공 API (/v1/*)"
    if uri.startswith("/images/"):
        return "/images/*"
    if uri == "/healthcheck":
        return "/healthcheck"
    return "미제공 경로"


def main():
    minutes = 5
    if len(sys.argv) > 1:
        try:
            minutes = int(sys.argv[1])
        except ValueError:
            print("사용법: python wafcheck.py [분]")
            sys.exit(2)

    logs = boto3.client("logs", region_name=REGION)
    end = int(time.time() * 1000)
    start = end - minutes * 60 * 1000

    total = Counter()          # action 별
    block_by_rule = Counter()  # BLOCK 의 terminatingRuleId 별
    block_by_class = Counter()  # BLOCK 의 경로분류 별
    block_api_by_rule = Counter()  # 제공 API 경로 BLOCK 을 규칙별
    samples = []               # 제공 API 경로 BLOCK 샘플

    try:
        paginator = logs.get_paginator("filter_log_events")
        for page in paginator.paginate(logGroupName=LOG_GROUP, startTime=start, endTime=end):
            for ev in page.get("events", []):
                try:
                    d = __import__("json").loads(ev["message"])
                except Exception:
                    continue
                action = d.get("action", "?")
                total[action] += 1
                if action != "BLOCK":
                    continue
                rule = d.get("terminatingRuleId", "?")
                uri = d.get("httpRequest", {}).get("uri", "")
                cls = path_class(uri)
                block_by_rule[rule] += 1
                block_by_class[cls] += 1
                if cls.startswith("제공 API"):
                    block_api_by_rule[rule] += 1
                    if len(samples) < 8:
                        args = d.get("httpRequest", {}).get("args", "")
                        method = d.get("httpRequest", {}).get("httpMethod", "")
                        samples.append(f"{method} {uri}?{args}"[:120])
    except logs.exceptions.ResourceNotFoundException:
        print(f"❌ 로그 그룹 '{LOG_GROUP}' 없음 (WAF 로깅 미설정 또는 트래픽 없음).")
        sys.exit(1)

    print(f"\n=== WAF 분석 (최근 {minutes}분, {LOG_GROUP}) ===\n")
    tot = sum(total.values())
    if tot == 0:
        print("  로그 없음 (트래픽 미발생 또는 샘플링 지연).")
        return
    allow = total.get("ALLOW", 0)
    block = total.get("BLOCK", 0)
    print(f"  전체 {tot}건 | ALLOW {allow} | BLOCK {block} ({block*100//tot}%)")

    print("\n [BLOCK 규칙별]")
    for rule, c in block_by_rule.most_common():
        print(f"  {rule:<24} {c}")

    print("\n [BLOCK 경로분류별]")
    for cls, c in block_by_class.most_common():
        flag = ""
        if cls.startswith("제공 API"):
            flag = "  ← 정상 오차단 의심 (값 크면 규칙 완화 검토)"
        if cls == "미제공 경로":
            flag = "  ← 404 여야 함 (AllowNonApiPaths 확인)"
        print(f"  {cls:<18} {c}{flag}")

    if block_api_by_rule:
        print("\n [제공 API 경로 BLOCK — 규칙별] (자기-403 핵심 지표)")
        for rule, c in block_api_by_rule.most_common():
            hint = " → 헤더 화이트리스트 의심" if "Header" in rule else (" → uuid/param 형식 규칙 빡셈 의심" if "Default" in rule else "")
            print(f"  {rule:<24} {c}{hint}")
        print("\n  샘플:")
        for s in samples:
            print(f"    {s}")
    print()


if __name__ == "__main__":
    main()
