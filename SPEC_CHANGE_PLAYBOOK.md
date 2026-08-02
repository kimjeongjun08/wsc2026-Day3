# 스펙 바뀔 때 — 그대로 따라치기

> 대회 중 종이로 "API/필드/경로가 바뀜" 공지가 오면 이 문서대로만.
> **안 바뀌면 아무것도 안 건드림.** 지금이 검증된 최적.

---

## 🟢 딱 3가지만 기억

1. **대부분 변경 = `turn.py`의 "요청 줄"만 고치면 됨** (WAF는 대개 그대로 통과)
2. **앱/dump 교체 = 환경변수(`MYSQL_*`) 확인** (README 맨 위 §0)
3. **정상이 막히는 것 같으면 = `waf.tf` L106 `block{}`→`allow{}` 한 줄** (가용성부터 방어)

---

## 📍 고칠 "요청 줄"은 여기 (`tools/turn.py`)

```
L148  await s.post(f"{base}/v1/user",    json={..., "username": u, "email": f"{u}@t.org"})   ← POST user
L149  await s.post(f"{base}/v1/product", json={..., "id": p, "name": p, "price": 1})          ← POST product
L159  session.get(f"{base}/v1/user?email={seed_u}@t.org&requestid=...&uuid=...")              ← GET user
L162  session.get(f"{base}/v1/product?id={seed_p}&requestid=...&uuid=...")                     ← GET product
L165  session.post(f"{base}/v1/stress",  json={..., "length": random.randint(50,200)})        ← POST stress
```
> `scaler.py`도 똑같은 요청이 L281(user)·L282(product)에 있음 — 아래 예시에서 같이 고침.

---

## ✍️ 예시로 그대로 따라치기

### 예시 1 — 필드 추가 (예: user에 `age` 추가) ★제일 흔함, 제일 쉬움

**종이:** `POST /v1/user` 바디에 `"age": 30` 이 추가됨.

**고칠 것:** `turn.py` L148 요청에 `age`만 끼워넣기. (그리고 `scaler.py` L281도 동일)

`turn.py` L148 — **콤마 + `"age": 30` 만 추가:**
```python
# 전(before)
await s.post(f"{base}/v1/user", json={"requestid": rid(), "uuid": uid(), "username": u, "email": f"{u}@t.org"})
# 후(after)  ← 맨 끝 } 앞에 , "age": 30  추가
await s.post(f"{base}/v1/user", json={"requestid": rid(), "uuid": uid(), "username": u, "email": f"{u}@t.org", "age": 30})
```

`scaler.py` L281도 똑같이 `, "age": 30` 추가.

**규칙 (네가 물어본 것):**
- **숫자면** `"age": 30` / **글자면** `"age": "값"` (따옴표) / **참거짓이면** `"age": true`
- 항상 **앞에 콤마 `,`** 찍고, `"이름": 값` 형태. 맨 끝 `}` **앞에** 넣기.

**WAF는?** → **안 건드려도 됨.** 필드 하나 늘어난 건 그냥 통과함.
**끝.** `python turn.py <엔드포인트>` 실행해서 측정표에 **에러 0**이면 성공.

---

### 예시 2 — 필드 이름 변경 (예: `email` → `user_email`)

**종이:** 필드 이름이 `email`에서 `user_email`로 바뀜.

`turn.py`에서 **`email` 글자를 전부 `user_email`로** 바꾸기 (L148 바디, L159 쿼리):
```python
# L148  "email": ...  →  "user_email": ...
await s.post(f"{base}/v1/user", json={..., "username": u, "user_email": f"{u}@t.org"})
# L159  ?email=  →  ?user_email=
session.get(f"{base}/v1/user?user_email={seed_u}@t.org&requestid={rid()}&uuid={uid()}")
```
`scaler.py` L281도 `email`→`user_email`.

**WAF는?** → email을 **형식검증**하니 이름도 바꿔야 함. `waf.tf`에서 `"email"` 3군데를 `"user_email"`로:
- `Ctrl+F`로 `name = "email"` 찾아 → `name = "user_email"` (GET 검증)
- `single_query_argument { name = "email" }` → `name = "user_email"`
> 못 찾겠거나 헷갈리면 → **급하면 폴백(맨 아래)**으로 가용성부터 살리고 천천히.

---

### 예시 3 — 값 형식이 바뀜 (예: `id`가 이제 숫자만)

`waf.tf` **L35** 한 줄만:
```hcl
# 전
id_regex = "^[a-zA-Z0-9_.:-]+$"
# 후 (숫자만)
id_regex = "^[0-9]+$"
```
그리고 `turn.py` seed 값이 새 형식 위반이면 바꾸기 (예: `p = "12345"`).

---

### 예시 4 — 아예 새 경로 (예: `/v1/order`) ★이건 손이 좀 감

여긴 표만 보고, 안 되면 폴백. (자세한 스니펫은 이 문서 하단 optional 참고)
- `waf.tf` L26 `waf_post_exact`에 `"/v1/order"` 추가
- `alb.tf` user 라우팅 블록 복사 → order로
- `turn.py`·`scaler.py`에 order 요청 추가

---

### 예시 5 — 필드가 **없어짐** (예: `requestid`를 더 안 보냄)

- `turn.py`/`scaler.py`: 그 필드를 요청에서 **빼기** (없어도 앱이 무시하면 안 빼도 됨)
- **⚠ WAF 주의**: 없어진 필드가 **형식검증 대상**이었으면(`email`/`id`/`requestid`) → WAF가 "그 필드 있어야 통과"라 **정상이 403 될 수 있음.**
  → `waf.tf` AllowValid에서 그 필드 검증 `statement { ... }` 블록을 **지우거나**, 헷갈리면 **폴백(allow)**.

### 예시 6 — 메서드가 바뀜 (예: product 수정이 `PUT`→`PATCH`)

- **WAF**: `waf.tf`에서 `search_string = "PUT"` 찾아 → `"PATCH"`로 (AllowValidPUT)
- **turn.py**: 해당 요청 메서드 바꾸기
- 헷갈리면 → 폴백

### 그 외 (헤더 추가·바디 구조 대변경·새 앱 등) — 드묾

정확히 못 짚겠는 이상한 변경이면 → **바로 폴백(allow)으로 가용성부터 확보**하고, 시간 되면 천천히.
**어떤 변경이든 폴백이 있어 전멸(0점)은 안 남.** 이게 2층 안전망의 핵심.

---

## 🚨 급할 때 = 딱 한 줄 (정상이 막히면 무조건 이것)

`terraform/waf.tf` **L106**:
```hcl
  default_action {
    allow {}          # ← block {} 를 allow {} 로
  }
```
그리고:
```bash
cd terraform && terraform apply -target=aws_wafv2_web_acl.cloudfront -var=... 
```
**효과:** 정상 트래픽 **무조건 통과**(가용성 산다). 공격은 시그니처+404로 계속 차단(점수 살짝↓).
**원칙: 만점보다 가용성 먼저. 시간 생기면 위 예시로 정확히 고친 뒤 `block{}`로 복귀.**

---

## ✅ 바꾼 뒤 확인 (항상)

```bash
python turn.py <엔드포인트>          # 측정표 에러 0 이면 요청 OK
kubectl get pods -n apdev             # 전부 Running (앱/dump 바꿨으면 특히)
# 정상요청 200/201, /v1/none 404, 공격 403 인지 curl 한 번
```

---

### (optional) 예시 4 새 경로 상세 스니펫

`alb.tf` — user 블록 복붙 후 이름/경로만 order로:
```hcl
resource "aws_lb_target_group" "order" { /* aws_lb_target_group.user 내용 복사, 이름만 order */ }
resource "aws_lb_listener_rule" "order" {
  # listener_arn 은 user 룰과 동일하게
  priority = 40
  action    { type = "forward"; target_group_arn = aws_lb_target_group.order.arn }
  condition { path_pattern { values = ["/v1/order*"] } }
}
```
`turn.py` L219 `["user","product","stress"]` → `[..., "order"]` + `_hit`에 order 요청 추가.
`scaler.py` L40 `APPS`에 `"order"` 추가.
> 복잡하면 무리하지 말고 **폴백(allow)**으로 가용성 확보가 우선.
