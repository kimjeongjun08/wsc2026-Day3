"""
update_waf.py — '헤더 이름' 화이트리스트 (커버리지 게이팅판)

■ 개선점 (기존 3분 고정 샘플링의 문제 해결)
  기존: 3분간 본 헤더만 화이트리스트 → 그 안에 안 온 요청조합(예: PUT /v1/product)이
        나중에 오면 헤더가 화이트리스트에 없어 정상인데 차단됨.
  개선: (메서드,경로) 조합별로 '봤는지'를 추적 → 기대 조합을 '다 볼 때까지' 대기 후 룰 생성.
        · 다 보면 각 조합의 헤더가 전부 수집된 상태라 정상 안 막힘.
        · 시간 상한(MAX_WAIT) 내 못 본 조합은 enforce 대상에서 제외 → 그 조합은 절대 안 막힘.
        · BASE_ALLOW(표준 헤더)는 안전망으로 항상 허용.

■ 왜 화이트리스트인가
  · 비정상에 구멍이 없다(default block → 명시 허용 외 전부 차단). 블랙리스트는 새 공격 놓침.
  · 경로/메서드/바디/공격패턴은 waf.tf가 담당. 여기선 '헤더 이름'만.

■ HPA/정상 보호
  · 헤더 '이름'만 검사(값 아님) → 정상 헤더값에 우연히 키워드 껴도 자폭 안 함.
  · enforce는 '충분히 관측된 (메서드,경로)'에만 적용 → 관측 안 된 정상요청은 통과.

사용법: python update_waf.py [--auto] [--min 분(기본3)] [--wait 분(최대,기본20)]
        · 최소 --min 분 동안은 6개 조합을 다 봐도 계속 수집(더 많은 헤더/UA 확보) 후 확정.
        · --min 안에 다 못 보면 다 볼 때까지(최대 --wait 까지) 계속.
        해제: python update_waf.py --remove
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
MAX_WAIT = 1200            # 커버리지 최대 대기(초). 이 안에 다 안 오면 본 것만 enforce.
MIN_WAIT = 180             # ★최소 수집 시간(초, 기본 3분). 6개 조합을 다 봐도 이 시간까지는 계속 받아
                           #   더 많은 헤더/UA 를 모은 뒤 확정한다(작은 표본으로 성급히 룰 만들면
                           #   나중에 온 정상 헤더/UA 를 못 봐서 차단할 위험 → 그걸 줄인다).

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
    """기대 (메서드,경로)를 다 볼 때까지 + '최소 min_wait 초' 까지 샘플링하며 조합별 헤더 수집.
       종료 = (모든 조합 관측 AND 최소시간 경과) 또는 max_wait 도달.
       반환: seen = {(method,path): {...}}"""
    if not allow_metrics:
        print("⚠ ACL에 Allow 룰이 없어 샘플 대상이 없음."); return {}
    print(f"⏳ 요청 조합 커버리지 수집 (최소 {min_wait//60}분 / 최대 {max_wait//60}분, {SAMPLE_EVERY}초 간격)")
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
        min_ok = elapsed >= min_wait
        note = "" if (missing or min_ok) else f" | 다 봤지만 최소 {min_wait//60}분까지 수집 계속"
        print(f"  [{elapsed:>4}s] 커버 {len(covered)}/{len(EXPECTED)} | 남은: {[f'{m} {p}' for m,p in missing] or '없음 ✅'}{note}")
        # 종료: 모든 조합 관측 + 최소시간 경과. (다 봐도 최소시간 전이면 계속 받는다.)
        if not missing and min_ok:
            print(f"  ✅ 모든 조합 관측 + 최소 {min_wait//60}분 경과 → 룰 생성")
            return seen
        if elapsed >= max_wait:
            print(f"  ⏱ 시간 상한({max_wait//60}분) 도달 → 관측된 {len(covered)}개 조합만 enforce, 미관측은 면제(안 막음)")
            return seen
        time.sleep(SAMPLE_EVERY)   # ★다음 폴링까지 대기(없으면 busy-loop 로 최소시간을 못 채우고 API 폭주)


def build_header_rule(enforce_pairs, allowed_headers, allowed_values):
    """헤더 화이트리스트 룰(priority 0):
       1) enforce_pairs 요청에서 화이트리스트에 없는 헤더 이름 → 403
       2) 특정 헤더(user-agent 등)의 값이 수집된 화이트리스트와 불일치 → 403
       두 조건을 OR로 묶어 하나의 룰에서 처리."""
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

    # 조건2: 특정 헤더 값 화이트리스트 (user-agent 등)
    # user-agent 값이 수집된 패턴과 불일치하면 차단
    value_checks = []
    # 값 검증 대상: user-agent (가장 중요)
    ua_values = allowed_values.get("user-agent", set())
    if ua_values:
        # 수집된 UA 값들로 regex 패턴 생성 (prefix 매칭 — "curl/", "python-requests/" 등)
        # 각 UA의 첫 단어(슬래시 전)를 prefix로 사용
        ua_prefixes = set()
        for v in ua_values:
            prefix = v.split("/")[0].split(" ")[0].strip()
            if prefix and len(prefix) >= 2:
                ua_prefixes.add(prefix)
        if ua_prefixes:
            # 허용 패턴: ^(curl|python|aiohttp|...)
            ua_regex = "^(" + "|".join(sorted(ua_prefixes)) + ")"
            # NOT match → 차단. WAF에서 NOT은 NotStatement로.
            value_checks.append({"NotStatement": {"Statement": {
                "RegexMatchStatement": {
                    "RegexString": ua_regex,
                    "FieldToMatch": {"SingleHeader": {"Name": "user-agent"}},
                    "TextTransformations": [{"Priority": 0, "Type": "LOWERCASE"}]
                }
            }}})

    # 최종 룰: scope AND (알 수 없는 이름 OR 값 불일치)
    if value_checks:
        block_condition = {"OrStatement": {"Statements": [anomaly_name] + value_checks}}
    else:
        block_condition = anomaly_name

    # ★ Priority 6: base가 0~2(KnownBadInputs/BlockAttacks/BlockUnknownPath) + Allow는 10~12로 미뤄둠.
    #   Allow는 terminating이라 헤더룰이 Allow보다 뒤면 정상경로 요청에서 firing 못 함(404남) → 반드시 Allow(10~12)
    #   앞(gap 3~9)에 둬야 함. 그래서 6. (waf.tf가 이 gap을 비워둠)
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
    if "--wait" in sys.argv:
        try:
            max_wait = int(sys.argv[sys.argv.index("--wait") + 1]) * 60
        except (ValueError, IndexError):
            pass
    min_wait = MIN_WAIT
    if "--min" in sys.argv:
        try:
            min_wait = int(sys.argv[sys.argv.index("--min") + 1]) * 60
        except (ValueError, IndexError):
            pass
    min_wait = min(min_wait, max_wait)   # 최소는 최대를 못 넘는다

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
        print(f"허용 User-Agent 값: {sorted(allowed_values['user-agent'])}")

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

    install(build_header_rule(enforce_pairs, allowed, allowed_values))
    print("\n✅ 적용 완료. enforce 조합에서 허용 외 헤더 이름/값 → 403.")
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
