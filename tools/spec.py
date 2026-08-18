"""spec.py — 대회 API 스펙 단일 소스(Single Source of Truth).

대회 중 경로/메소드/쿼리/바디가 바뀌면 **여기만** 고치고 `python apply_spec.py --apply` 실행.
→ waf.tf의 경로 locals + AllowValidGET/POST/PUT 규칙이 자동 재생성됨(정상만 통과·비정상 차단 유지).

과제지 기준 현재 스펙. 각 엔드포인트:
  path       : URI 경로 (정확 일치)
  get_query  : {쿼리파라미터명: 검증regex키}  — GET 허용조건 (값 형식 검증). 없으면 경로만.
  post_body  : [바디에 반드시 포함될 문자열들]  — POST 허용조건. 없으면 POST 불가.
  put        : True면 PUT 허용(메소드+경로만; multipart라 바디검증 불가).
"""

# 값 검증 regex (get_query가 참조)
REGEX = {
    "email": r"^[a-zA-Z0-9._%+=-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$",  # xxx@xxx.xxx
    "id":    r"^[a-zA-Z0-9_.:-]+$",                                  # grade-xxx-pN, seed_123 등
    # 새 파라미터 형식이 필요하면 여기에 추가하고 get_query에서 참조
}

# 엔드포인트 정의 (과제지 기준)
ENDPOINTS = {
    "user": {
        "path": "/v1/user",
        "get_query": {"email": "email"},          # GET /v1/user?email=... (email 형식 검증)
        "post_body": ["username", "@"],            # POST body에 username + @ 포함
    },
    "product": {
        "path": "/v1/product",
        "get_query": {"id": "id"},                 # GET /v1/product?id=... (id 형식 검증)
        "post_body": ["name", "price"],            # POST body에 name + price
        "put": True,                               # PUT /v1/product (이미지 업로드)
    },
    "stress": {
        "path": "/v1/stress",
        "post_body": ["length"],                   # POST body에 length
    },
}

# GET 프리픽스 허용 (경로만, 값검증 없음) — 이미지 정적경로
PREFIXES = ["/images/"]

# 튜닝/스케일링툴이 다루는 앱 목록 (앱 이름이 바뀌면 여기도)
APPS = ["user", "product", "stress"]


# ── 파생값 (apply_spec.py가 사용) ──
def get_paths():
    return [e["path"] for e in ENDPOINTS.values() if "get_query" in e or e.get("get_only_path")]

def post_paths():
    return [e["path"] for e in ENDPOINTS.values() if "post_body" in e]

def put_paths():
    return [e["path"] for e in ENDPOINTS.values() if e.get("put")]
