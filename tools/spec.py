"""spec.py — 대회 API 스펙 (tools/fix/spec.json 에서 자동 생성. 직접 고치지 말 것).

바꾸려면 tools/fix/spec.json 을 고치고 `python3 tools/fix/spec.py --apply` 를 실행해라.
"""

REGEX = {
    "email": "^[a-zA-Z0-9._%+=-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$",
    "id": "^[a-zA-Z0-9_.:-]+$",
}

ENDPOINTS = {
    "user": {
        "path": "/v1/user",
        "get_query": {"email": "email"},
        "post_body": ["username", "@"],
    },
    "product": {
        "path": "/v1/product",
        "get_query": {"id": "id"},
        "post_body": ["name", "price"],
        "put": True,
    },
    "stress": {
        "path": "/v1/stress",
        "post_body": ["length"],
    },
}

PREFIXES = ["/images/"]

APPS = ["user", "product", "stress"]


def get_paths():
    return [e["path"] for e in ENDPOINTS.values() if "get_query" in e]

def post_paths():
    return [e["path"] for e in ENDPOINTS.values() if "post_body" in e]

def put_paths():
    return [e["path"] for e in ENDPOINTS.values() if e.get("put")]
