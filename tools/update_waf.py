"""
update_waf.py
1. waf/ 폴더 JSON rules를 WAF ACL에 적용 (default BLOCK)
2. 5분간 sampled requests 수집 → 헤더 화이트리스트 룰 자동 추가

사용법: python update_waf.py
"""
import boto3
import json
import sys
import time
from datetime import datetime, timedelta, timezone

REGION = "us-east-1"
ACL_NAME = "apdev-cf-acl"
SCOPE = "CLOUDFRONT"
SAMPLE_DURATION = 90  # 1분 30초


def get_acl(waf):
    acls = waf.list_web_acls(Scope=SCOPE)["WebACLs"]
    acl = next((a for a in acls if a["Name"] == ACL_NAME), None)
    if not acl:
        print(f"ERROR: WAF ACL '{ACL_NAME}' not found")
        sys.exit(1)
    return acl["Id"], acl["ARN"]


def collect_headers(waf, acl_arn):
    print(f"\n⏳ {SAMPLE_DURATION//60}분간 헤더 수집 중... (정상 트래픽이 들어와야 합니다)")
    time.sleep(SAMPLE_DURATION)

    now = datetime.now(timezone.utc)
    start = now - timedelta(seconds=SAMPLE_DURATION + 60)
    # {header_name: set(values)} 형태로 수집
    headers_map = {}

    # 화이트리스트에 넣지 말아야 할 헤더 '이름'(소문자, 정확 일치). 이 헤더가 들어온
    # 요청은 차단 상태로 둔다. 기존 코드는 헤더 NAME 과 VALUE 모두에 substring 매칭을
    # 해서, 정상 헤더의 이름/값이 우연히 토큰을 '포함'하기만 해도(예: 값에 "token"/"bot"/
    # "vector" 포함, 이름에 "x-forwarded" 포함) 화이트리스트에서 빠지고 → 그 정상 요청이
    # BlockUnknownHeaders 로 403(자기 DoS). 정확 이름 매칭으로 교체한다.
    BLOCK_HEADER_NAMES = {
        "authorization", "x-hacker", "x-attack", "x-custom", "x-evil",
        "x-forwarded-for", "x-forwarded-host", "x-real-ip", "x-originating-ip",
        "x-client-ip", "true-client-ip", "x-remote-ip",
    }

    for metric in ["AllowValidGET", "AllowValidPOST", "AllowValidPUT"]:
        try:
            resp = waf.get_sampled_requests(
                WebAclArn=acl_arn, RuleMetricName=metric, Scope=SCOPE,
                TimeWindow={"StartTime": start, "EndTime": now},
                MaxItems=100,
            )
            for sample in resp.get("SampledRequests", []):
                for h in sample["Request"].get("Headers", []):
                    name = h["Name"].lower()
                    value = h.get("Value", "")
                    # 이름 정확 일치만으로 제외. 절대 VALUE 기준으로 제외하지 않음.
                    if name in BLOCK_HEADER_NAMES:
                        continue
                    if name not in headers_map:
                        headers_map[name] = set()
                    headers_map[name].add(value[:80])  # 값 80자까지만
        except Exception:
            pass

    return headers_map


def build_header_rule(allowed_headers):
    return {
        "Name": "BlockUnknownHeaders",
        "Priority": 0,
        "Action": {"Block": {}},
        "Statement": {
            "AndStatement": {
                "Statements": [
                    {
                        "OrStatement": {
                            "Statements": [
                                {"ByteMatchStatement": {"SearchString": "/v1/", "FieldToMatch": {"UriPath": {}}, "PositionalConstraint": "STARTS_WITH", "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
                                {"ByteMatchStatement": {"SearchString": "/images/", "FieldToMatch": {"UriPath": {}}, "PositionalConstraint": "STARTS_WITH", "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
                                {"ByteMatchStatement": {"SearchString": "/healthcheck", "FieldToMatch": {"UriPath": {}}, "PositionalConstraint": "EXACTLY", "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
                            ]
                        }
                    },
                    {
                        "RegexMatchStatement": {
                            "RegexString": "^.*",
                            "FieldToMatch": {
                                "Headers": {
                                    "MatchPattern": {"ExcludedHeaders": allowed_headers},
                                    "MatchScope": "ALL",
                                    "OversizeHandling": "CONTINUE",
                                }
                            },
                            "TextTransformations": [{"Priority": 0, "Type": "NONE"}],
                        }
                    },
                ]
            }
        },
        "VisibilityConfig": {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True, "MetricName": "BlockUnknownHeaders"},
    }


def main():
    waf = boto3.client("wafv2", region_name=REGION)
    acl_id, acl_arn = get_acl(waf)
    print(f"WAF ACL: {ACL_NAME} ({acl_id})\n")

    # 헤더 수집 → 화이트리스트 추가
    print("=== 헤더 화이트리스트 룰 추가 ===")
    headers_map = collect_headers(waf, acl_arn)

    if not headers_map:
        print("❌ 헤더 수집 실패 (트래픽 없음). 나중에 다시 실행하세요.")
        return

    print(f"\n감지된 헤더 ({len(headers_map)}개):")
    for name, values in sorted(headers_map.items()):
        sample_vals = list(values)[:3]
        vals_str = ", ".join(sample_vals)
        if len(values) > 3:
            vals_str += f" ... (+{len(values)-3})"
        print(f"  {name:<25} = {vals_str}")

    # 제외할 헤더 입력
    exclude = input("\n제외할 헤더 (쉼표 구분, 없으면 엔터): ").strip()
    if exclude:
        for h in exclude.split(","):
            h = h.strip().lower()
            if h in headers_map:
                del headers_map[h]
                print(f"  제외됨: {h}")

    # 표준 클라이언트/CDN 헤더는 항상 허용. 90초 샘플링 창에 안 잡힌 정상 헤더가
    # 나중에 들어와 403 되는 자기 DoS 를 막는다(공격 헤더는 위 BLOCK_HEADER_NAMES 로 차단).
    BASELINE_HEADERS = {
        "host", "user-agent", "accept", "accept-encoding", "accept-language",
        "accept-charset", "content-type", "content-length", "connection",
        "cache-control", "pragma", "origin", "referer", "cookie", "date",
        "upgrade-insecure-requests", "te", "dnt",
        "sec-fetch-mode", "sec-fetch-site", "sec-fetch-dest", "sec-fetch-user",
        "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
        "via", "x-amz-cf-id", "x-amzn-trace-id", "x-forwarded-proto", "x-forwarded-port",
    }
    allowed_headers = sorted(set(headers_map.keys()) | BASELINE_HEADERS)
    print(f"\n최종 허용 헤더 ({len(allowed_headers)}개): {allowed_headers}")

    confirm = input("\n이 헤더로 화이트리스트 적용? (y/n): ").strip().lower()
    if confirm != "y":
        print("건너뜀"); return

    header_rule = build_header_rule(allowed_headers)

    # 기존 WAF rules 유지 + header rule 추가/교체
    resp = waf.get_web_acl(Name=ACL_NAME, Scope=SCOPE, Id=acl_id)
    lock_token = resp["LockToken"]
    current_rules = resp["WebACL"]["Rules"]
    vis = resp["WebACL"]["VisibilityConfig"]
    default_action = resp["WebACL"]["DefaultAction"]

    # 기존 BlockUnknownHeaders 제거 후 새로 추가
    rules = [r for r in current_rules if r["Name"] != "BlockUnknownHeaders"]
    rules.append(header_rule)

    waf.update_web_acl(
        Name=ACL_NAME, Scope=SCOPE, Id=acl_id,
        LockToken=lock_token,
        DefaultAction=default_action,
        VisibilityConfig=vis,
        Rules=rules,
    )
    print(f"\n✅ 헤더 화이트리스트 적용 완료")
    print(f"   허용 헤더: {allowed_headers}")
    print(f"   이 외 헤더 포함된 API 요청 → 403")


if __name__ == "__main__":
    main()
