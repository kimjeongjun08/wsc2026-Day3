# xtune — 비상용 '추가 애플리케이션' 튜너

대회에 **4번째 애플리케이션**이 나올 때를 위한 도구다. 원본 튜너(`tools/tuner`)는
user/product/stress 3개 전용이라 새 앱의 최적값을 못 잡는다. xtune 은 **그 새 앱 하나만**
따로 벤치마크·사이징·적용한다. 새 앱이 뭘 하는지(DB/DDB/stress류든) 몰라도 **직접 부하를 걸어
재서** 정한다. 새 앱을 붙이는 데 필요한 인프라 파일(deploy/svc/hpa/tgb + terraform)도
`scaffold` 로 **템플릿에서 생성**해 준다(원본은 안 건드림).

> ⚠ 아마 안 나올 확률이 높다. 혹시 몰라 준비하는 비상용이다.
> 원본 튜너(`tools/tuner`)는 **절대 안 건드린다**. xtune 은 새 앱 자원만 손댄다.

---

## 원본과 동시에 돌려도 안 꼬인다

원본 `tuner/apply.sh` 는 `apdev-pool` / `apdev-stress-pool` 의 초과 NodeClaim 을 **삭제(회수)** 한다.
그래서 새 앱 X 를 그 두 풀에 올리면 원본이 X 의 노드를 지워버린다. xtune 은 이걸 피한다:

1. **대상은 X 뿐** — `user/product/stress` 는 아예 대상 거부(guard).
2. **X 는 '원본이 회수 안 하는 노드'에만** 올린다 (아래 배치 3가지).
3. 원본 `solve.py` 는 미지 앱 트래픽을 `[skip]` 처리한다(코드 확인) → 원본 감시 루프도 안 깨진다.
4. 프로브 파드(`xtune-probe`)·상태파일(`.xtune-state-<app>`)·측정파일(`xcurve-<app>.json`) 전부
   원본과 다른 이름 → 파일/파드 충돌 없음.
5. 원본 `lib.sh` 를 안 읽는다(채점서버 자격증명 불필요) → 독립 실행.

---

## 배치 3가지 (원본 baseline = MNG1[user+product] + stress1 = **2대**)

| 모드 | 배치 | baseline | 언제 (recommend 가 자동 판정) |
|---|---|---|---|
| **pack-app** | `(user+product+X) \| (stress)` — X 를 **MNG 노드**에 | **2** | io 성격 + 가벼움 (CPU 요구 ≲ 1.4코어) |
| **pack-stress** | `(user+product) \| (stress+X)` — X 를 **stress 노드**에 | **2** | CPU 성이나 매우 가벼움 (≲ 0.5코어) |
| **iso** | `(user+product) \| (stress) \| (X 전용)` | **3+** | 무거움 (전용 노드 필요) |

- **pack-app**: MNG(관리형 고정 노드)는 원본이 회수 안 한다. user/product 가 지연민감(200ms)이라
  X 도 가볍고 io 성격일 때만 얹는다. 무겁거나 CPU-burst 면 user/product 지연을 해친다.
- **pack-stress**: 항상 존재하는 stress 노드에 동거. stress SLO 가 관대(1s)라 CPU 성 가벼운 X 에 적합.
  stress 도 CPU-burn 이라 둘 다 무거워지면 서로 굶는다 → 그땐 iso.
- **iso**: X 전용 Karpenter 노드풀(`<prefix>-xtune-pool`, stress 풀을 클론). 원본이 안 건드린다.
  무거운 X 의 안전한 정답. `baseline 3대 시작`이 이것이다.

> **baseline 2 로 성능이 안 나오면 iso 로 올려라.** 원본 3앱 자체의 baseline 을 2→3 으로 올리는
> 판단은 원본 튜너(`solve.py --min-nodes`)가 별도로 한다 — xtune 은 X 만 책임진다.

---

## 0) 새 앱 인프라 파일 만들기 (scaffold)

새 앱이 나오면 붙이는 데 필요한 파일이 많다: k8s(deploy/svc/hpa/tgb) + terraform(ECR·ALB
target group·listener) + WAF + setup.sh + DB. `scaffold` 는 `templates/` 의 샘플을 받은 정보로
**치환해 `generated/` 로 뽑고**, 손으로 반영할 것을 **체크리스트로** 출력한다. **원본은 안 건드린다.**

```bash
cd tools/xtune
APATH=/v1/order PORT=8080 PRIORITY=40 ./xtune.sh scaffold order
```

생성물(`generated/`):

| 파일 | 용도 | 적용 |
|---|---|---|
| `xapp-order.tf` | ECR repo + ALB target group + listener rule(`/v1/order*`) | `terraform/` 로 복사 → `terraform apply` (자동 로드, 기존 .tf 무수정) |
| `order-deploy.yaml` | Deployment (배치는 비워둠 → xtune apply 가 넣음) | `kubectl apply -f` |
| `order-service.yaml` | Service (`order-svc`) | `kubectl apply -f` |
| `order-hpa.yaml` | HPA | `kubectl apply -f` |
| `order-tgb.yaml` | TargetGroupBinding (ALB↔파드) | ARN 채우고 `kubectl apply -f` |
| `order-spec-endpoint.py` | **WAF** 엔드포인트 스니펫 | `tools/spec.py` 의 `ENDPOINTS` 에 붙여넣기 |

체크리스트가 짚어주는 **손으로 반영할 곳**: ①앱 바이너리 배치 ②`xapp-*.tf` 복사 ③**WAF**(아래) ④`setup.sh`
ECR 빌드 루프·DB 테이블 ⑤`terraform apply`. 옵션(env): `APATH PORT HEALTH PRIORITY IMAGE REQ UTIL MAXREP`.
API 를 알면 `GETQ="id:id" POSTBODY="name price" PUT=1` 로 WAF 스니펫까지 채워진다. AWS 접근 가능하면
IMAGE·TG ARN 도 실제값으로 자동 치환된다.

### ★ WAF 는 반드시 추가해야 한다 (안 하면 정상 트래픽이 다 막힘)

WAF(`waf.tf`)는 **엄격 화이트리스트 + default block** 이다. 새 경로 `/v1/order` 를 안 넣으면
정상 트래픽이 **404**(BlockUnknownPath) 또는 **403**(default block)으로 막힌다. scaffold 는 붙여넣을
스니펫(`generated/order-spec-endpoint.py`)을 만들어 준다. 두 가지가 **다** 필요하다:

```bash
# 1) generated/order-spec-endpoint.py 를 tools/spec.py 의 ENDPOINTS 에 붙여넣고
cd tools && python apply_spec.py --apply
#    → ① waf.tf 경로 locals(SPEC:PATHS) 자동 갱신        = 404 방지
#    → ② generated_waf_rules.tf.txt 에 AllowValid HCL 생성 → waf.tf 에 반영해야 = 403 방지
cd ../terraform && terraform apply     # waf.tf 반영
```

> scaffold 는 waf.tf 를 직접 안 건드린다. WAF 반영은 검증된 `spec.py`+`apply_spec.py` 파이프라인에
> 맡긴다(경로 locals 자동, AllowValid 는 생성물 검토 후 반영). 이게 이 프로젝트의 안전한 WAF 갱신 방식이다.

## 사용 흐름 (인프라가 붙은 뒤 튜닝)

원본 튜너로 3앱을 세팅(baseline 2)하고, 위 scaffold 로 새 앱을 배포한 상태에서:

```bash
cd tools/xtune

# 1) 새 앱 X 의 처리 한계를 실측 (동시성→지연/처리량 곡선). POST 면 BODY 필수.
BODY='{"requestid":"r","uuid":"u","field":"__N__"}' ./xtune.sh measure order

# 2) 곡선으로 배치(pack-app/pack-stress/iso)·HPA·requests·baseline 추천
TARGET_RPS=60 ./xtune.sh recommend order      # 피크 rps 추정을 주면 노드/max 까지 계산

# 3) 추천대로 적용 (X 자원만 바뀐다)
./xtune.sh apply order pack-app               # 또는 pack-stress / iso [노드수]

# 4) 되돌리기 (모드 자동 판별)
./xtune.sh remove order
```

`measure` 는 X 의 deploy/hpa 만 잠깐 1개로 줄여 재고 원복한다(원본 3앱은 안 건드림).
`apply` 는 X 의 nodeSelector/toleration·HPA(min2/max/util)·cpu requests 만 바꾼다. pack 은 소극적
request(동거용), iso 는 eager request(전용노드 선점)로 자동 분리한다.

---

## 옵션 (env)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `NS` | `apdev` | 네임스페이스 |
| `DEPLOY` | `<app>` | Deployment 이름 |
| `SVC` | `<app>-svc:8080` | 클러스터 내부 서비스 host |
| `LABEL` | `app=<app>` | 파드 라벨 셀렉터 (CPU 실측·파드 조회용) |
| `HPA` | `<app>-hpa` | HPA 이름 |
| `APATH` | `/v1/<app>` | API 경로 |
| `METHOD` | `POST` | `POST` 또는 `GET` |
| `BODY` | (없음) | POST 바디. 요청마다 유니크가 필요하면 `__N__` 을 넣어라(카운터로 치환). **POST 면 필수.** |
| `QKEY` `QVAL` | (없음) | GET 조회키/값 (존재하는 행을 조회해야 404 안 뜬다) |
| `SLA_MS` | `1000` | 이 앱의 지연 SLO(ms). 포화 판정 기준 |
| `TARGET_RPS` | (없음) | 피크 rps 추정. recommend/apply 가 HPA max·노드수·baseline 산정에 씀 |
| `LEVELS` | `1 2 4 8 16 32` | 측정 동시성 단계 |
| `DUR` | `10` | 단계당 측정 초 |
| `REQ` `UTIL` `MAXREP` | (자동) | apply 에서 수동 오버라이드 |

---

## 예시

```bash
# GET 위주의 조회 앱 (existing row 를 id 로 조회)
METHOD=GET QKEY=id QVAL=seed_1 SLA_MS=200 ./xtune.sh measure lookup
TARGET_RPS=40 ./xtune.sh recommend lookup
./xtune.sh apply lookup pack-app          # 가벼운 io → MNG 동거, baseline 2

# 무거운 계산 앱
BODY='{"n":500}' SLA_MS=1000 ./xtune.sh measure crunch
TARGET_RPS=25 ./xtune.sh recommend crunch
./xtune.sh apply crunch iso 2             # 무거움 → 전용 2대, baseline 4
```

---

## 한계 (알고 쓸 것)

- **라이브 실행은 대회 클러스터에서** 해야 한다(kubectl + Karpenter 필요). 로컬에선 로직만 검증됨.
- 측정은 X 를 파드 1개로 줄이므로 **X 에 트래픽이 흐르는 중엔 하지 마라**(셋업 시점에).
- pack 모드는 **가벼운 X 전용**이다. X 가 스케일하며 무거워지면 동거 노드가 부족해지고
  (pack-stress 는 원본의 stress 노드 회수와 충돌할 수도) → 그럴 땐 `iso` 로 올려라.
- `apply` 는 X 의 원래 nodeSelector/toleration 이 없다고 가정한다(새 앱은 보통 그렇다).
  원래 있었다면 `remove` 후 수동 확인.
- HPA 가 없으면 자동 생성하지 않는다(안내만) — `kubectl autoscale` 로 만들어라.

---

## 파일

| 파일 | 역할 |
|---|---|
| `xtune.sh` | 전부. scaffold / measure / recommend / apply / remove |
| `templates/` | 새 앱 인프라 샘플(deploy/svc/hpa/tgb.yaml.tmpl, xapp.tf.tmpl). scaffold 가 치환 |
| `generated/` | scaffold 가 뽑은 실제 파일(`<app>-*.yaml`, `xapp-<app>.tf`). 여길 apply |
| `xcurve-<app>.json` | measure 결과(동시성 곡선). recommend 가 읽음 |
| `.xtune-state-<app>` | apply 가 남기는 모드/풀 상태. remove 가 읽음 |
