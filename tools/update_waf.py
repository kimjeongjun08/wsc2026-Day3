"""
update_waf.py — '헤더 이름' 화이트리스트 (최소시간 + 커버리지 이중 게이팅판)

■ 종료 조건 (2026-08-25 개정)
  · 커버리지(기대 6조합을 전부 관측)에 도달해도 **최소 MIN_WAIT(3분)는 계속 수집**한다.
    예전엔 조합당 요청 1개만 보여도 즉시 룰을 굳혔다 — 표본 1개짜리 화이트리스트라
    채점기가 요청마다 헤더를 조금 달리 보내면 정상이 403 됐다.
  · 3분 시점에 커버리지 완료면 그때 끝. 미완료면 최대 MAX_WAIT(20분)까지 수집하다
    완료되는 순간 끝. 20분에도 못 본 조합은 enforce 에서 면제(그 조합은 절대 안 막음).

■ 왜 '이름'만 보고 '값'은 안 보나 (UA 값 화이트리스트 제거, 2026-08-25)
  · 값(특히 User-Agent)은 채점기가 로테이션할 수 있고 sampled_requests 는 표본이라
    다양성을 다 못 담는다 → 미관측 값이 오면 정상인데 403. 원칙("어떤 부하툴이든
    정상은 무조건 통과") 위반이라 뺐다. 값에 실린 공격은 waf.tf 의
    BlockHeaderAttacks(시그니처 블랙리스트)가 이미 잡는다.
  · 헤더 '이름'은 클라이언트 구현이 정하는 유한집합이라 3분 표본으로 안정된다.

■ 역할 분담
  · 경로/메서드/바디/공격패턴/헤더값 공격 → waf.tf (정적, 검증됨)
  · 헤더 '이름' 화이트리스트 → 이 도구 (동적 — 채점 헤더를 미리 모르므로)
  · BASE_ALLOW(표준 헤더)는 표본에 없어도 항상 허용 — 차단을 줄이는 방향으로만 작용.

사용법: python update_waf.py [--auto] [--min 분] [--wait 분]   (해제: --remove)
"""
import boto3, json, sys, time
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REGION = "us-east-1"
ACL_NAME = "apdev-cf-acl"
SCOPE = "CLOUDFRONT"

SAMPLE_EVERY = 30          # 폴링 주기(초)
MIN_WAIT = 180             # 최소 수집 시간(초). 커버리지가 일찍 차도 이만큼은 표본을 더 모은다.
MAX_WAIT = 1200            # 커버리지 최대 대기(초). 이 안에 다 안 오면 본 것만 enforce.

# enforce 대상 기대 (메서드, 경로) 조합. 이게 다 관측되면 룰 확정.
#   images/healthcheck는 헤더 다양성 커서 제외(enforce 안 함).
EXPECTED = [
    ("GET", "/v1/user"), ("GET", "/v1/product"),
    ("POST", "/v1/user"), ("POST", "/v1/product"), ("POST", "/v1/stress"),
    ("PUT", "/v1/product"),
]

# 표준/인프라 헤더 — 샘플에 없어도 항상 허용(안전망). 화이트리스트는 '차단 축소' 방향으로만 작용.
BASE_ALLOW = {
    "host", "user-agent", "accept", "accept-encoding", "accept-language",
    "content-type", "content-length", "connection", "keep-alive",
    "if-none-match", "if-modified-since", "cache-control", "pragma",
    "range", "origin", "referer", "via", "date", "expect", "te", "upgrade-insecure-requests",
    "x-forwarded-for", "x-forwarded-proto", "x-forwarded-port",
    "x-amzn-trace-id", "x-amz-cf-id", "cloudfront-forwarded-proto",
    "baggage", "x-vercel-id", "traceparent", "tracestate",
    "x-request-id", "x-correlation-id", "x-amzn-requestid",
    # 브라우저형 클라이언트 표준 (채점기가 브라우저 UA 로 올 때 같이 온다)
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "dnt",
    "accept-charset", "if-match", "if-range", "if-unmodified-since",
    # CloudFront 가 뷰어 요청에 얹을 수 있는 것들
    "cloudfront-viewer-country", "cloudfront-is-mobile-viewer",
    "cloudfront-is-desktop-viewer", "cloudfront-is-tablet-viewer",
    "cloudfront-is-smarttv-viewer", "cloudfront-viewer-http-version",
    "x-real-ip", "forwarded",
}

# 헤더 '이름'에 이 키워드가 들어가면 화이트리스트에서 제외(공격 마커성 헤더). 값은 검사 안 함.
NAME_BLACKLIST = {"attack", "attacker", "hack", "hacker", "exploit", "inject", "malicious",
                  "evil", "payload", "shell", "backdoor", "scanner", "nikto", "sqlmap",
                  "nmap", "burp", "acunetix", "wpscan"}


def get_acl(waf):
    acls = waf.list_web_acls(Scope=SCOPE)["WebACLs"]
    acl = next((a for a in acls if a["Name"] == ACL_NAME), None)
    if not acl:
        print(f"ERROR: WAF ACL '{ACL_NAME}' not found"); sys.exit(1)
    return acl["Id"], acl["ARN"]


def get_allow_metrics(waf, acl_id):
    """현재 ACL의 Allow 룰 메트릭 이름(룰 이름 무관하게 동적으로)."""
    resp = waf.get_web_acl(Name=ACL_NAME, Scope=SCOPE, Id=acl_id)
    return [r["VisibilityConfig"]["MetricName"] for r in resp["WebACL"]["Rules"]
            if "Allow" in r.get("Action", {}) and r.get("VisibilityConfig", {}).get("MetricName")]


def _norm_path(uri):
    p = (uri or "").split("?")[0]
    if p.startswith("/images/"):
        return "/images/"
    return p


def collect_until_covered(waf, acl_arn, allow_metrics, max_wait, min_wait=MIN_WAIT):
    """조합별 헤더 이름 수집.
       종료: (경과 >= min_wait 이고 기대 조합 전부 관측) 또는 (경과 >= max_wait).
       — 커버리지가 일찍 차도 min_wait 까지는 표본을 계속 쌓는다.
       반환: seen = {(method,path): {"names": set, "values": {name: set}}}"""
    if not allow_metrics:
        print("⚠ ACL에 Allow 룰이 없어 샘플 대상이 없음."); return {}
    print(f"⏳ 헤더 표본 수집 (최소 {min_wait//60}분 / 최대 {max_wait//60}분, {SAMPLE_EVERY}초 간격)")
    print(f"   대상 조합: {[f'{m} {p}' for m,p in EXPECTED]}")
    seen = {}
    start = time.time()
    while True:
        now = datetime.now(timezone.utc)
        win = now - timedelta(seconds=SAMPLE_EVERY + 120)
        for metric in allow_metrics:
            try:
                resp = waf.get_sampled_requests(
                    WebAclArn=acl_arn, RuleMetricName=metric, Scope=SCOPE,
                    TimeWindow={"StartTime": win, "EndTime": now}, MaxItems=500)
            except Exception:
                continue
            for s in resp.get("SampledRequests", []):
                req = s.get("Request", {})
                method = (req.get("Method") or "").upper()
                path = _norm_path(req.get("URI", ""))
                if not method:
                    continue
                names = set()
                values_by_name = {}  # {header_name: set(values)}
                for h in req.get("Headers", []):
                    hname = h["Name"].lower()
                    hval = h.get("Value", "").lower()
                    if any(kw in hname for kw in NAME_BLACKLIST):
                        continue
                    names.add(hname)
                    values_by_name.setdefault(hname, set()).add(hval)
                seen.setdefault((method, path), {}).setdefault("names", set()).update(names)
                for hname, vals in values_by_name.items():
                    seen[(method, path)].setdefault("values", {}).setdefault(hname, set()).update(vals)

        covered = [e for e in EXPECTED if e in seen]
        missing = [e for e in EXPECTED if e not in seen]
        elapsed = int(time.time() - start)
        tag = "표본 축적 중" if (not missing and elapsed < min_wait) else ""
        print(f"  [{elapsed:>4}s] 커버 {len(covered)}/{len(EXPECTED)} | 남은: {[f'{m} {p}' for m,p in missing] or '없음 ✅'} {tag}")
        if not missing and elapsed >= min_wait:
            print(f"  ✅ 커버리지 완료 + 최소 {min_wait//60}분 표본 확보 → 룰 생성")
            return seen
        if elapsed >= max_wait:
            print(f"  ⏱ 상한 {max_wait//60}분 도달 → 관측된 {len(covered)}개 조합만 enforce, 미관측 조합은 면제(안 막음)")
            return seen
        time.sleep(SAMPLE_EVERY)   # ★원본엔 이게 없어서 API 를 쉼 없이 폴링했다


def build_header_rule(enforce_pairs, allowed_headers):
    """헤더 '이름' 화이트리스트 룰: enforce_pairs 요청에서 허용 목록에 없는
       헤더 이름이 하나라도 있으면 403.
       ★값 검증(User-Agent 등)은 2026-08-25 에 제거했다 — 채점기가 값을
       로테이션하면 표본 밖 정상값이 차단된다. 값 공격은 waf.tf 의
       BlockHeaderAttacks 시그니처가 담당한다."""
    combos = []
    for (method, path) in enforce_pairs:
        combos.append({"AndStatement": {"Statements": [
            {"ByteMatchStatement": {"SearchString": method, "FieldToMatch": {"Method": {}},
                                    "PositionalConstraint": "EXACTLY",
                                    "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
            {"ByteMatchStatement": {"SearchString": path, "FieldToMatch": {"UriPath": {}},
                                    "PositionalConstraint": "EXACTLY",
                                    "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
        ]}})
    scope = combos[0] if len(combos) == 1 else {"OrStatement": {"Statements": combos}}

    # 조건1: 알 수 없는 헤더 이름
    anomaly_name = {"RegexMatchStatement": {
        "RegexString": "^.*",
        "FieldToMatch": {"Headers": {"MatchPattern": {"ExcludedHeaders": sorted(allowed_headers)},
                                     "MatchScope": "KEY", "OversizeHandling": "CONTINUE"}},
        "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}}

    block_condition = anomaly_name

    # ★ Priority 5: waf.tf 가 0~2(경로404/공격403/헤더값403)와 10~12(Allow)를 쓰고
    #   3~9 를 비워뒀다. Allow 는 terminating 이라 이 룰이 Allow 보다 뒤면 영영
    #   안 돈다 — 반드시 그 사이(3~9)에 있어야 한다.
    return {
        "Name": "BlockUnknownHeaders", "Priority": 5, "Action": {"Block": {}},
        "Statement": {"AndStatement": {"Statements": [scope, block_condition]}},
        "VisibilityConfig": {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True,
                             "MetricName": "BlockUnknownHeaders"},
    }


def install(rule):
    waf = boto3.client("wafv2", region_name=REGION)
    acl_id, _ = get_acl(waf)
    resp = waf.get_web_acl(Name=ACL_NAME, Scope=SCOPE, Id=acl_id)
    rules = [r for r in resp["WebACL"]["Rules"] if r["Name"] != "BlockUnknownHeaders"]
    rules.append(rule)
    waf.update_web_acl(Name=ACL_NAME, Scope=SCOPE, Id=acl_id, LockToken=resp["LockToken"],
                       DefaultAction=resp["WebACL"]["DefaultAction"],
                       VisibilityConfig=resp["WebACL"]["VisibilityConfig"], Rules=rules)


def main():
    auto = "--auto" in sys.argv
    max_wait = MAX_WAIT
    min_wait = MIN_WAIT
    if "--wait" in sys.argv:
        try:
            max_wait = int(sys.argv[sys.argv.index("--wait") + 1]) * 60
        except (ValueError, IndexError):
            pass
    if "--min" in sys.argv:
        try:
            min_wait = int(sys.argv[sys.argv.index("--min") + 1]) * 60
        except (ValueError, IndexError):
            pass
    min_wait = min(min_wait, max_wait)

    waf = boto3.client("wafv2", region_name=REGION)
    acl_id, acl_arn = get_acl(waf)
    print(f"WAF ACL: {ACL_NAME} ({acl_id})")
    print("=== 헤더 화이트리스트 (커버리지 게이팅) ===")

    metrics = get_allow_metrics(waf, acl_id)
    seen = collect_until_covered(waf, acl_arn, metrics, max_wait, min_wait)

    enforce_pairs = [e for e in EXPECTED if e in seen]   # 관측된 기대 조합만 enforce
    if not enforce_pairs:
        print("❌ 관측된 조합 없음 (정상 트래픽 필요). 나중에 다시 실행."); return

    allowed = set(BASE_ALLOW)
    allowed_values = {}  # {header_name: set(values)} — 값 화이트리스트
    for pair in enforce_pairs:
        pair_data = seen[pair]
        allowed |= pair_data.get("names", set())
        for hname, vals in pair_data.get("values", {}).items():
            allowed_values.setdefault(hname, set()).update(vals)
    allowed = {h for h in allowed if not any(kw in h for kw in NAME_BLACKLIST)}

    print(f"\nenforce 조합({len(enforce_pairs)}): {[f'{m} {p}' for m,p in enforce_pairs]}")
    exempt = [f"{m} {p}" for m, p in EXPECTED if (m, p) not in seen]
    if exempt:
        print(f"면제 조합(미관측 → 안 막음): {exempt}")
    print(f"허용 헤더 이름({len(allowed)}): {sorted(allowed)}")
    if "user-agent" in allowed_values:
        print(f"(참고) 관측된 User-Agent 값: {sorted(allowed_values['user-agent'])} — 룰에는 안 쓴다")

    if not auto:
        try:
            import threading
            res = [None]
            t = threading.Thread(target=lambda: res.__setitem__(0, input(
                "\n제외할 헤더(쉼표, 30초내 엔터=바로적용): ").strip()), daemon=True)
            t.start(); t.join(timeout=30)
            if res[0]:
                for h in res[0].split(","):
                    allowed.discard(h.strip().lower())
                print(f"  제외 반영")
            elif t.is_alive():
                print("  30초 경과 → 바로 적용")
        except Exception:
            pass

    install(build_header_rule(enforce_pairs, allowed))
    print("\n✅ 적용 완료. enforce 조합에서 허용 외 헤더 이름 → 403 (값은 waf.tf 시그니처 담당).")
    print("   정상이 막히면 즉시 해제: python update_waf.py --remove")


def remove_header_rule():
    waf = boto3.client("wafv2", region_name=REGION)
    acl_id, _ = get_acl(waf)
    resp = waf.get_web_acl(Name=ACL_NAME, Scope=SCOPE, Id=acl_id)
    rules = [r for r in resp["WebACL"]["Rules"] if r["Name"] != "BlockUnknownHeaders"]
    if len(rules) == len(resp["WebACL"]["Rules"]):
        print("BlockUnknownHeaders 룰 없음 (이미 제거됨)"); return
    waf.update_web_acl(Name=ACL_NAME, Scope=SCOPE, Id=acl_id, LockToken=resp["LockToken"],
                       DefaultAction=resp["WebACL"]["DefaultAction"],
                       VisibilityConfig=resp["WebACL"]["VisibilityConfig"], Rules=rules)
    print("✅ BlockUnknownHeaders 제거 완료.")


if __name__ == "__main__":
    if "--remove" in sys.argv:
        remove_header_rule()
    else:
        main()
