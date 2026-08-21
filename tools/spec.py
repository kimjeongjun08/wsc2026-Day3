"""spec.py — 대회 API 스펙 단일 소스(SSOT) 로더.

★실제 SSOT 는 옆의 spec.json 이다(경로·메서드·api 만 담음). 이 파일은 그걸 읽어
기존 툴이 쓰는 형태(ENDPOINTS/REGEX/PREFIXES/APPS)로 펼친다. spec.json 없으면 폴백.

spec.json 은 단순하게 유지하고(경로/메서드/쿼리/바디 필드만), 'WAF 가 뭘 검사할지'는
아래 **관례**로 파생한다 → content_type/multipart/정규식 같은 디테일을 JSON 에 안 둔다.

WAF 파생 관례:
  · 쿼리 형식검증 : 파라미터 이름이 'email'/'id' 인 것만 정규식 검증(나머지 requestid/uuid 등은 통과).
  · 바디 필수마커 : 바디필드 중 어디나 있는 것(requestid/uuid/id)은 빼고, 'email' 은 '@'(이메일 형식)로.
                    → user: [username, @] · product: [name, price] · stress: [length] (현재 waf.tf 와 동일).
  · PUT           : 메서드에 PUT 이 있으면 허용(경로+메서드만; multipart 라 바디검증 불가).
"""
import json as _json
import os as _os

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_JSON = _os.path.join(_HERE, "spec.json")

# ── WAF 값 검증 정규식 (waf.tf 의 id_regex/email_regex 와 대응. 이름으로만 참조) ──
_REGEX = {
    "email": r"^[a-zA-Z0-9._%+=-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$",
    "id":    r"^[a-zA-Z0-9_.:-]+$",
}
# ── WAF 파생 관례 ──
_VALIDATE_PARAMS = {"email": "email", "id": "id"}   # 이 이름의 쿼리 파라미터만 형식검증
_COMMON_BODY = {"requestid", "uuid", "id"}          # 어디나 있는 필드 → WAF 바디마커에서 제외


def _get_query(query):
    return {p: _VALIDATE_PARAMS[p] for p in query if p in _VALIDATE_PARAMS}


def _body_markers(fields):
    out = []
    for f in fields:
        if f in _COMMON_BODY:
            continue
        out.append("@" if f == "email" else f)      # email 필드는 '@' 포함(이메일 형식)으로 검사
    return out


# ── 하드코딩 폴백 (spec.json 없을 때만) ──
_FALLBACK_ENDPOINTS = {
    "user":    {"path": "/v1/user",    "get_query": {"email": "email"}, "post_body": ["username", "@"]},
    "product": {"path": "/v1/product", "get_query": {"id": "id"}, "post_body": ["name", "price"], "put": True},
    "stress":  {"path": "/v1/stress",  "post_body": ["length"]},
}
_FALLBACK_PREFIXES = ["/images/"]


def _load():
    """spec.json → (REGEX, PREFIXES, ENDPOINTS, APPS, RAW). 없으면 폴백."""
    if not _os.path.exists(_JSON):
        eps = {k: dict(v) for k, v in _FALLBACK_ENDPOINTS.items()}
        return dict(_REGEX), list(_FALLBACK_PREFIXES), eps, list(eps.keys()), None
    with open(_JSON, encoding="utf-8") as f:
        raw = _json.load(f)
    prefixes = list(raw.get("prefixes", []))
    apps = raw.get("apps", {})
    endpoints = {}
    for name, a in apps.items():
        e = {"path": a["path"]}
        methods = a.get("methods", {})
        if "GET" in methods:
            gq = _get_query(methods["GET"].get("query", []))
            if gq:
                e["get_query"] = gq
        if "POST" in methods:
            pb = _body_markers(methods["POST"].get("body", []))
            if pb:
                e["post_body"] = pb
        if "PUT" in methods:
            e["put"] = True
        endpoints[name] = e
    return dict(_REGEX), prefixes, endpoints, list(apps.keys()), raw


REGEX, PREFIXES, ENDPOINTS, APPS, RAW = _load()


# ── 파생값 (apply_spec.py 등이 사용) ──
def get_paths():
    return [e["path"] for e in ENDPOINTS.values() if "get_query" in e or e.get("get_only_path")]

def post_paths():
    return [e["path"] for e in ENDPOINTS.values() if "post_body" in e]

def put_paths():
    return [e["path"] for e in ENDPOINTS.values() if e.get("put")]


def app_conf(name):
    """spec.json 의 apps[name] 원본(methods 등). 없으면 {}."""
    if RAW:
        return RAW.get("apps", {}).get(name, {})
    return {}


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(f"스펙 소스: {'spec.json' if RAW else '하드코딩 폴백'}")
    for a in APPS:
        e = ENDPOINTS[a]
        ms = ",".join((app_conf(a).get("methods") or {}).keys()) or "?"
        print(f"  {a}: path={e['path']} methods={ms} "
              f"get_query={e.get('get_query', '-')} post_body={e.get('post_body', '-')} put={e.get('put', False)}")
