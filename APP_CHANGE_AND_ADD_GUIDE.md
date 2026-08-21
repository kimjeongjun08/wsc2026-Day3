# 앱 변경 / 추가 가이드 (wsc2026-Day3 — 현재 툴 기준)

대회 중 **기존 앱이 바뀌거나(변경)** **새 앱이 생기면(추가)** 무엇을 어디서 하는지.
SSOT 는 `tools/spec.json` (경로·메서드·api 만). `terraform apply` 전에 미리 돌려두고 한 번에 apply.

> 안전 원칙: 검증된 원본은 최대한 안 건드린다. 툴은 스냅샷 대비 **바뀐 것만** 앵커드 치환하고,
> `.bak` 백업 + `terraform validate` 게이트 + dry-run 기본이라 오염·꼬임이 없다.

---

## 0. 파일 지도

| 관심사 | 파일 |
|---|---|
| **API SSOT** (경로/메서드/필드) | `tools/spec.json` |
| SSOT 로더(관례로 WAF 파생) | `tools/spec.py` (직접 안 건드림) |
| **변경 자동화** (원커맨드) | `tools/apply_change.py` |
| WAF 동기화(경로 locals + AllowValid 생성) | `tools/apply_spec.py` |
| WAF / ALB / CloudFront | `terraform/waf.tf` / `alb.tf` / `cloudfront.tf` |
| 원본 튜너(3앱) | `tools/tuner/` (변경 툴이 경로만 패치) |
| **추가 앱 스캐폴드 + 튜너** | `tools/xtune/` (`scaffold` + `measure/recommend/apply`) |
| 앱 바이너리 / 부트스트랩 | `terraform/application/<app>/<app>` / `terraform/setup.sh` |

---

## 1. 변경 (기존 앱) — `spec.json` 고치고 한 커맨드

### 1-1. `tools/spec.json` 을 새 스펙으로 수정
바꿀 앱의 `path` / `methods` 의 `query`·`body` 만 고친다. (예: `"path": "/v1/user"` → `"/v2/user"`,
또는 `"query": ["email",...]` → `["mail",...]`)

### 1-2. 변경 툴 실행
```bash
cd tools
python apply_change.py            # dry-run: 무엇이 바뀔지 diff 만
python apply_change.py --apply    # 적용(+.bak 백업 + terraform validate 게이트 + 스냅샷 갱신)
```
**JSON 하나 → 아래를 전부 자동 반영** (스펙 무변경이면 아무것도 안 바뀜):

| 변경 | 자동 |
|---|---|
| 경로 | `waf.tf`(경로 locals + AllowValid), `alb.tf`(path_pattern), `cloudfront.tf`(product 처럼 경로 behavior 있는 앱), `tuner/*.sh`(리터럴 + 전체 균일이동 시 `/prefix/$APP`) |
| 쿼리키 | `waf.tf`(single_query_argument) *(튜너 QKEY 는 위치 보고)* |
| 바디필드 | `waf.tf`(AllowValid body) *(튜너 바디는 위치 보고)* |

### 1-3. 배포
```bash
cd terraform && terraform apply   # 변경된 waf/alb/cloudfront 반영
```
> 앱 로직/바이너리가 바뀌었으면 `terraform/application/<app>/<app>` 교체, DB 스키마 바뀌었으면
> `terraform/setup.sh` 의 `CREATE TABLE` 확인 후 apply.

### 주의 (자동 아님 — 안전상 보고만)
- **튜너의 쿼리키/바디필드**: 하드코딩 형태가 제각각이라 자동치환 대신 **정확한 위치를 보고**한다 → 그 줄 수동 확인.
- **일부 앱만 경로가 바뀌면**: 튜너 concurrency/profile 의 제네릭 `/v1/$APP` 은 자동으로 못 바꾼다(다른 앱 깨짐) → 경고대로 수동. (전체 앱이 균일 이동하면 자동.)
- **구조 변경**(쿼리 파라미터를 새로 추가 등, rename 아님): `apply_spec.py` 가 만드는 `generated_waf_rules.tf.txt` 참고해 반영.

---

## 2. 추가 (새 4번째 앱) — `xtune scaffold`

새 앱 `order`, 경로 `/v1/order` 예시. (자세한 배치·튜닝은 `tools/xtune/README.md`.)

### 2-1. 인프라 파일 생성
```bash
cd tools/xtune
APATH=/v1/order PORT=8080 PRIORITY=40 GETQ="id" POSTBODY="name price" ./xtune.sh scaffold order
```
→ `generated/` 에: `xapp-order.tf`(ECR+TG+listener), `order-deploy/service/hpa/tgb.yaml`,
`order-spec-apps-entry.json`(WAF 스니펫).

### 2-2. WAF (★필수 — 안 하면 정상 트래픽 404/403)
```bash
# generated/order-spec-apps-entry.json 을 tools/spec.json 의 "apps" 에 붙여넣고
cd tools && python apply_spec.py --apply
#   ① waf.tf 경로 locals 자동(404 방지)
#   ② generated_waf_rules.tf.txt 의 AllowValid HCL → waf.tf 에 반영(403 방지)
```

### 2-3. 나머지
- `generated/xapp-order.tf` → `terraform/` 복사, 바이너리 `terraform/application/order/order` 배치.
- `terraform/setup.sh`: ECR 빌드 루프에 `order` 추가 + (필요시) DB 테이블.
- `cd terraform && terraform apply` → `kubectl apply -f generated/order-{deploy,service,hpa}.yaml` → TGB(ARN 채워) apply.

### 2-4. 튜닝 (배치)
```bash
cd tools/xtune
BODY='{"requestid":"r","uuid":"u","name":"__N__","price":1}' ./xtune.sh measure order
TARGET_RPS=<피크> ./xtune.sh recommend order       # pack-app(baseline2,MNG) / pack-stress(baseline2,stress) / iso(baseline3)
./xtune.sh apply order <mode>
```

---

## 3. 둘 다 발생

`tools/spec.json` 을 **한 번에** 고친다(바뀐 기존 앱 + 새 앱 apps 엔트리) → 순서:
```bash
cd tools
python apply_change.py --apply    # 기존 앱 변경분 → waf/alb/cloudfront/tuner
python apply_spec.py --apply      # 새 앱 포함 waf 경로 locals + AllowValid HCL 생성 → waf.tf 반영
# 새 앱 인프라: xtune scaffold 로 이미 뽑은 xapp.tf/매니페스트/바이너리/setup.sh
cd ../terraform && terraform apply
# 튜닝: tuner(3앱) + xtune(새 앱)
```

---

## 4. 긴급 폴백
정상이 막히면 `terraform/waf.tf` 의 `default_action { block {} }` → `allow {}` + `terraform apply`.
→ 정상 무조건 통과, 공격은 BlockAttacks/BlockUnknownPath 로 계속 차단. 만점보다 가용성 먼저.
(상세 예시는 `SPEC_CHANGE_PLAYBOOK.md`.)

---

## 5. 요약
- **변경**: `spec.json` 고침 → `apply_change.py --apply` → `terraform apply`. (waf/alb/cloudfront/tuner 자동)
- **추가**: `xtune scaffold` → spec.json apps 붙여넣기 → `apply_spec.py --apply` + waf 반영 → terraform/k8s apply → `xtune measure/recommend/apply`.
- 안전: dry-run 기본 · `.bak` 백업 · `terraform validate` 게이트 · 스냅샷 대비 바뀐 것만.
