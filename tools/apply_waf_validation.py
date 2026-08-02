"""
apply_waf_validation.py — WAF AllowValid 룰에 query/body 형식 검증 추가

작년 방식 기반, 느슨하게:
  - GET /v1/user: email= 포함, requestid 숫자
  - GET /v1/product: id= 포함, requestid 숫자
  - POST /v1/user: body에 requestid(숫자), uuid, email(@포함), username
  - POST /v1/product: body에 requestid, uuid, id, name, price
  - POST /v1/stress: body에 length
  - PUT /v1/product: 메소드+경로만 (multipart)
  - GET /images/*: 경로만

헤더 검사 없음 (update_waf.py 영역).
파라미터 개수 강제 없음.

사용법:
  python apply_waf_validation.py           → 적용
  python apply_waf_validation.py --remove  → 원래 느슨한 AllowValid로 되돌리기
"""
import boto3
import json
import sys

REGION = "us-east-1"
ACL_NAME = "apdev-cf-acl"
SCOPE = "CLOUDFRONT"

# UUID 느슨 (v4 강제 안 함 — 하이픈 포함 hex 문자열이면 OK)
UUID_REGEX = "^[0-9a-fA-F-]{20,40}$"
# requestid: 숫자만
REQID_REGEX = "^[0-9]+$"
# 이메일 느슨: @ 포함이면 OK
EMAIL_REGEX = "^[^@]+@[^@]+$"


def build_get_user():
    """GET /v1/user — email= 포함, requestid 숫자"""
    return {"AndStatement": {"Statements": [
        {"ByteMatchStatement": {"SearchString": "GET", "FieldToMatch": {"Method": {}},
            "PositionalConstraint": "EXACTLY",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"ByteMatchStatement": {"SearchString": "/v1/user", "FieldToMatch": {"UriPath": {}},
            "PositionalConstraint": "EXACTLY",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"ByteMatchStatement": {"SearchString": "email=", "FieldToMatch": {"QueryString": {}},
            "PositionalConstraint": "CONTAINS",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"RegexMatchStatement": {"RegexString": REQID_REGEX,
            "FieldToMatch": {"SingleQueryArgument": {"Name": "requestid"}},
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
    ]}}


def build_get_product():
    """GET /v1/product — id= 포함, requestid 숫자"""
    return {"AndStatement": {"Statements": [
        {"ByteMatchStatement": {"SearchString": "GET", "FieldToMatch": {"Method": {}},
            "PositionalConstraint": "EXACTLY",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"ByteMatchStatement": {"SearchString": "/v1/product", "FieldToMatch": {"UriPath": {}},
            "PositionalConstraint": "EXACTLY",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"ByteMatchStatement": {"SearchString": "id=", "FieldToMatch": {"QueryString": {}},
            "PositionalConstraint": "CONTAINS",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"RegexMatchStatement": {"RegexString": REQID_REGEX,
            "FieldToMatch": {"SingleQueryArgument": {"Name": "requestid"}},
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
    ]}}


def build_get_images():
    """GET /images/* — 경로 prefix만"""
    return {"AndStatement": {"Statements": [
        {"ByteMatchStatement": {"SearchString": "GET", "FieldToMatch": {"Method": {}},
            "PositionalConstraint": "EXACTLY",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"ByteMatchStatement": {"SearchString": "/images/", "FieldToMatch": {"UriPath": {}},
            "PositionalConstraint": "STARTS_WITH",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
    ]}}


def build_post_user():
    """POST /v1/user — body에 requestid(숫자), uuid, email(@), username"""
    return {"AndStatement": {"Statements": [
        {"ByteMatchStatement": {"SearchString": "POST", "FieldToMatch": {"Method": {}},
            "PositionalConstraint": "EXACTLY",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"ByteMatchStatement": {"SearchString": "/v1/user", "FieldToMatch": {"UriPath": {}},
            "PositionalConstraint": "EXACTLY",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"RegexMatchStatement": {"RegexString": REQID_REGEX,
            "FieldToMatch": {"JsonBody": {"MatchPattern": {"IncludedPaths": ["/requestid"]},
                "MatchScope": "VALUE", "InvalidFallbackBehavior": "NO_MATCH",
                "OversizeHandling": "CONTINUE"}},
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"ByteMatchStatement": {"SearchString": "username",
            "FieldToMatch": {"Body": {"OversizeHandling": "CONTINUE"}},
            "PositionalConstraint": "CONTAINS",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"ByteMatchStatement": {"SearchString": "@",
            "FieldToMatch": {"Body": {"OversizeHandling": "CONTINUE"}},
            "PositionalConstraint": "CONTAINS",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
    ]}}


def build_post_product():
    """POST /v1/product — body에 requestid(숫자), id, name, price"""
    return {"AndStatement": {"Statements": [
        {"ByteMatchStatement": {"SearchString": "POST", "FieldToMatch": {"Method": {}},
            "PositionalConstraint": "EXACTLY",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"ByteMatchStatement": {"SearchString": "/v1/product", "FieldToMatch": {"UriPath": {}},
            "PositionalConstraint": "EXACTLY",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"RegexMatchStatement": {"RegexString": REQID_REGEX,
            "FieldToMatch": {"JsonBody": {"MatchPattern": {"IncludedPaths": ["/requestid"]},
                "MatchScope": "VALUE", "InvalidFallbackBehavior": "NO_MATCH",
                "OversizeHandling": "CONTINUE"}},
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"ByteMatchStatement": {"SearchString": "name",
            "FieldToMatch": {"Body": {"OversizeHandling": "CONTINUE"}},
            "PositionalConstraint": "CONTAINS",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"ByteMatchStatement": {"SearchString": "price",
            "FieldToMatch": {"Body": {"OversizeHandling": "CONTINUE"}},
            "PositionalConstraint": "CONTAINS",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
    ]}}


def build_post_stress():
    """POST /v1/stress — body에 length"""
    return {"AndStatement": {"Statements": [
        {"ByteMatchStatement": {"SearchString": "POST", "FieldToMatch": {"Method": {}},
            "PositionalConstraint": "EXACTLY",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"ByteMatchStatement": {"SearchString": "/v1/stress", "FieldToMatch": {"UriPath": {}},
            "PositionalConstraint": "EXACTLY",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"ByteMatchStatement": {"SearchString": "length",
            "FieldToMatch": {"Body": {"OversizeHandling": "CONTINUE"}},
            "PositionalConstraint": "CONTAINS",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
    ]}}


def build_put_product():
    """PUT /v1/product — 메소드+경로만 (multipart body 검증 불가)"""
    return {"AndStatement": {"Statements": [
        {"ByteMatchStatement": {"SearchString": "PUT", "FieldToMatch": {"Method": {}},
            "PositionalConstraint": "EXACTLY",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        {"ByteMatchStatement": {"SearchString": "/v1/product", "FieldToMatch": {"UriPath": {}},
            "PositionalConstraint": "EXACTLY",
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
    ]}}


def build_allow_valid_rule():
    """AllowValid 룰 — 형식 검증 포함"""
    return {
        "Name": "AllowValid",
        "Priority": 10,
        "Action": {"Allow": {}},
        "Statement": {"OrStatement": {"Statements": [
            build_get_user(),
            build_get_product(),
            build_get_images(),
            build_post_user(),
            build_post_product(),
            build_post_stress(),
            build_put_product(),
        ]}},
        "VisibilityConfig": {
            "SampledRequestsEnabled": True,
            "CloudWatchMetricsEnabled": True,
            "MetricName": "AllowValid",
        },
    }


def build_loose_allow_valid():
    """원래의 느슨한 AllowValid 룰 (되돌리기용)"""
    return {
        "Name": "AllowValid",
        "Priority": 10,
        "Action": {"Allow": {}},
        "Statement": {"OrStatement": {"Statements": [
            # GET: user/product + /images/
            {"AndStatement": {"Statements": [
                {"ByteMatchStatement": {"SearchString": "GET", "FieldToMatch": {"Method": {}},
                    "PositionalConstraint": "EXACTLY",
                    "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
                {"RegexMatchStatement": {
                    "RegexString": "^(/v1/user|/v1/product)$|^(/images/)",
                    "FieldToMatch": {"UriPath": {}},
                    "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
            ]}},
            # POST: user/product/stress
            {"AndStatement": {"Statements": [
                {"ByteMatchStatement": {"SearchString": "POST", "FieldToMatch": {"Method": {}},
                    "PositionalConstraint": "EXACTLY",
                    "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
                {"RegexMatchStatement": {
                    "RegexString": "^(/v1/user|/v1/product|/v1/stress)$",
                    "FieldToMatch": {"UriPath": {}},
                    "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
            ]}},
            # PUT: product
            {"AndStatement": {"Statements": [
                {"ByteMatchStatement": {"SearchString": "PUT", "FieldToMatch": {"Method": {}},
                    "PositionalConstraint": "EXACTLY",
                    "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
                {"ByteMatchStatement": {"SearchString": "/v1/product",
                    "FieldToMatch": {"UriPath": {}},
                    "PositionalConstraint": "EXACTLY",
                    "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
            ]}},
        ]}},
        "VisibilityConfig": {
            "SampledRequestsEnabled": True,
            "CloudWatchMetricsEnabled": True,
            "MetricName": "AllowValid",
        },
    }


def apply(rule):
    waf = boto3.client("wafv2", region_name=REGION)
    acls = waf.list_web_acls(Scope=SCOPE)["WebACLs"]
    acl = next(a for a in acls if a["Name"] == ACL_NAME)
    resp = waf.get_web_acl(Name=ACL_NAME, Scope=SCOPE, Id=acl["Id"])
    lock = resp["LockToken"]

    # AllowValid 교체
    rules = [r for r in resp["WebACL"]["Rules"] if r["Name"] != "AllowValid"]
    rules.append(rule)

    waf.update_web_acl(
        Name=ACL_NAME, Scope=SCOPE, Id=acl["Id"], LockToken=lock,
        DefaultAction=resp["WebACL"]["DefaultAction"],
        VisibilityConfig=resp["WebACL"]["VisibilityConfig"],
        Rules=rules)


def main():
    if "--remove" in sys.argv:
        print("AllowValid → 느슨 모드 (메소드+경로만) 으로 되돌리기...")
        apply(build_loose_allow_valid())
        print("✅ 되돌리기 완료. 형식 검증 없이 메소드+경로만 체크.")
    else:
        print("AllowValid → 형식 검증 모드 적용 중...")
        print("  GET /v1/user: email= 포함, requestid 숫자")
        print("  GET /v1/product: id= 포함, requestid 숫자")
        print("  POST /v1/user: requestid(숫자), username, email(@)")
        print("  POST /v1/product: requestid(숫자), name, price")
        print("  POST /v1/stress: length")
        print("  PUT /v1/product: 메소드+경로만")
        print("  GET /images/*: 경로만")
        print("  (헤더 검사 없음 — update_waf.py에서 별도 처리)")
        apply(build_allow_valid_rule())
        print("\n✅ 적용 완료.")
        print("   정상 막히면: python apply_waf_validation.py --remove")


if __name__ == "__main__":
    main()
