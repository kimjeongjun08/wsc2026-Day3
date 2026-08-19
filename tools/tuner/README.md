# 인프라 최적값 튜너

주어진 앱 바이너리와 트래픽에 대해 **점수가 가장 높은 인프라 구성**(노드 수 + stress 배치)을
찾아서 적용한다. 앱에 손잡이(디버그 플래그 같은 것)를 요구하지 않고, 채점 서버에 접근하지도
않는다 — 필요한 건 전부 ALB 지표와 클러스터에서 직접 잰다.

x0.5 트래픽에서 **40.0/40 (100점)** 을 실측했고 15분 정식 회차로 2회 재현했다.

---

# 1. 대회에서 이 순서로 하면 된다

대회는 **인프라 구축에 1시간**을 주고, 그 뒤 트래픽이 들어온다.

## 준비 (`.env` 만들기)

```bash
cp .env.example .env && vi .env    # 채점 서버 주소·비밀번호·계정 (커밋 금지)
export AWS_PROFILE=<프로파일>
```

`.env` 는 회차 검증용(`verify.sh`)에만 필요하다. `autotune.sh` / `pretune.sh` 는 없어도 된다.

## 0~20분 — 인프라 배포

```bash
cd terraform && terraform apply -auto-approve
```

RDS Multi-AZ 가 병목이다(실측 16분). **가장 먼저 시작**하고 기다리는 동안 다음을 준비한다.

## 20~25분 — bastion 제거 + kubectl 연결

```bash
aws eks update-kubeconfig --region ap-northeast-2 --name apdev-cluster
kubectl -n apdev get pods                       # 3개 앱 Running 확인
terraform apply -var bastion_enabled=false      # bastion 제거
```

**bastion 제거는 점수에 직결된다.** 비용 지표는 EKS 노드가 아니라 계정의 running EC2
**전체 수**라서 bastion 한 대가 비용 2점이다. 게다가 t3.small 이라 "t3.medium 타입만"
규정도 어긴다. EKS API 가 퍼블릭이라 이후 제어는 로컬 kubectl 로 된다.

## 25~40분 — 자동 준비

```bash
./autotune.sh prepare
```

이게 알아서 한다:

1. **동시성 곡선 측정** (앱·메서드당 약 1분) — "이 앱은 2코어로 초당 몇 개까지 처리하는가"
2. **콜드 스타트 구성 적용** — 하한 2대 / 상한 6대 / stress 동거 / stress requests 100m
3. **안정화될 때까지 대기** — 아래 6가지가 전부 통과할 때까지

## 40~55분 — 자체 부하로 최적값 확정 (시간이 되면)

```bash
./pretune.sh "2:shared 3:shared 3:iso 4:iso2"
```

트래픽이 오기 전에 **직접 부하를 넣어** 후보 구성들을 실제로 비교한다.
공개 엔드포인트로 쏘므로 CloudFront → WAF → ALB → 파드 전 구간이 검증된다.

> ⚠️ **부하 크기를 실제 트래픽에 맞춰야 순위가 맞다.** 자체 부하가 97rps 였을 때
> `2노드/shared` 가 37.0 으로 나왔는데, 같은 구성이 채점 x0.5(47rps)에서는 40.0 이다.
> 트래픽 양을 모르는 상태라면 한 지점만 재지 말고 `pretable.sh` 로 여러 rps 표를 만들어라.
>
> ⚠️ POST 는 DB 에 행을 만든다. 과제지가 "발생하는 트래픽 외 임의의 데이터 삽입"을
> 경계하므로 기본 POST 비율을 10% 로 제한했다. 필요 이상으로 돌리지 마라.
> 실측 참고: 후보 하나당 40초 부하에 POST 약 475건.

여러 부하 수준에서 표를 만들려면:

```bash
./pretable.sh "30 60 120"     # rps 별 최적 구성 표
```

## 55~60분 — 마지막 확인

```bash
./autotune.sh ready           # 안정화 6종 확인
```

그리고 **채점 플랫폼에 엔드포인트를 등록한다** (미등록이면 0점).
등록 후 확인할 때는 브라우저 User-Agent 를 써라 — 기본 curl UA 는 WAF 에 막혀 403 이 난다.

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST <엔드포인트>/v1/user \
  -H 'Content-Type: application/json' -H 'User-Agent: Mozilla/5.0' \
  -d '{"requestid":"r","uuid":"u","username":"probe1","email":"probe1@x.com"}'
```

## 트래픽 시작 — 켜두고 끝

```bash
./autotune.sh run > autotune.log 2>&1 &
```

사람이 더 할 일은 없다. 가끔 로그만 본다.

---

# 2. 트래픽 중에 무엇이 어떻게 도는가

트래픽은 오르내린다(실측: 베이스라인 user 2rps ↔ 스파이크 22rps, **11배**).
그래서 하나의 고정 구성이 정답일 수 없다. 세 층이 각자 다른 속도로 움직인다.

| 층 | 반응 속도 | 무엇을 바꾸나 | 요청 유실 |
|---|---|---|---|
| **HPA + Karpenter** | 초 ~ 1분 | 파드 수, 노드 수 (하한~상한 사이) | 없음 |
| **autotune 감시** | 1분 | 과부하 시 노드 상한 즉시 상향 | 없음 (파드 안 건드림) |
| **autotune 구조** | 10분 이상 | `minDomains`, stress 배치 | 롤아웃 발생 |

**핵심은 자주 하는 조정이 파드를 안 건드린다는 것이다.** 노드 상한(`NodePool.limits.cpu`)만
바꾸면 기존 파드는 그대로고 Karpenter 가 노드만 붙인다. Deployment 를 패치하는 구조 변경은
롤링 재시작을 부르므로 10분 쿨다운을 걸어 드물게 한다.

`autotune.sh run` 이 매 주기에 하는 일:

1. **과부하 확인** — ALB `TargetResponseTime` 이 SLA 를 넘으면 계산을 기다리지 않고 즉시 증설
2. **곡선 자기보정** — 곡선 예측과 ALB 실측이 어긋나면 계수를 갱신
   (요청 본문·앱 버전·DB 크기 같은 숨은 변수를 통째로 흡수한다)
3. **최적 구성 계산** — ALB 에서 읽은 실제 rps 로 다시 계산하고, 충분히 좋아질 때만 적용

## 하한과 상한은 왜 분리하나

```bash
./apply.sh 2 shared 6      # 하한 2대, 상한 6대
```

- **하한**(`minDomains`) — 항상 이만큼은 유지. 비용 만점 지점이자 고가용성 최소선.
- **상한**(`NodePool.limits.cpu`) — 스파이크 때 여기까지 자동 증설 허용.

상한까지 하한과 같게 묶으면 스파이크에 노드를 못 만든다. 실측: 그 조건에서 stress 가
26% 로 떨어져 비용 게이트가 터졌고 **22.5점**이었다.

비용은 구간 **평균**이라 따라가는 쪽이 항상 이긴다:

```
베이스라인 2대 + 스파이크 4대  →  평균 3.3대
계속 4대                      →  평균 4.0대   (약 1.4점 손해)
```

---

# 3. 명령어 요약

| 명령 | 언제 | 무엇을 |
|---|---|---|
| `./autotune.sh prepare` | 트래픽 전 1회 | 곡선 측정 + 콜드 스타트 + 안정화 대기 |
| `./autotune.sh run &` | 트래픽 시작 시 | 감시·보정·조정 루프 (켜두면 끝) |
| `./autotune.sh show` | 아무때나 | 지금 트래픽과 추천 구성만 출력 (적용 안 함) |
| `./autotune.sh ready` | 아무때나 | 안정화 6종 확인 |
| `./pretune.sh "<후보들>"` | 트래픽 전 | 자체 부하로 후보 구성 실측 비교 |
| `./pretable.sh "<rps들>"` | 트래픽 전 | rps 별 최적 구성 표 생성 |
| `./apply.sh <하한> <배치> [상한]` | 수동 | 구성 고정 |
| `./tune_requests.sh <requests> [limits]` | 수동 | stress CPU 지분 조정 |
| `./concurrency.sh <앱> [post\|get]` | 수동 | 동시성-지연 곡선 측정 |
| `python3 solve.py --traffic '<json>'` | 수동 | 최적 구성 계산만 |
| `./verify.sh <배수> [분]` | 연습용 | 채점 회차 실행 (`.env` 필요) |

`<배치>` 는 `shared`(stress 동거) 또는 `iso`·`iso2`·`iso3`(stress 전용 노드 1·2·3대).

## 안정화 확인 6종 (`autotune.sh ready`)

```
[O] 모든 Deployment 준비됨          원하는 수 == 준비된 수
[O] Pending 파드 없음               있으면 노드가 모자라거나 제약이 안 맞는 것
[O] 노드 수가 목표와 일치           Karpenter 가 만들거나 지우는 중이 아님
[O] 모든 노드 Ready
[O] ALB 타깃 전부 healthy           실제로 트래픽을 받을 수 있는지의 최종 판정
[O] 마지막 구성 변경 후 2분 경과     방금 바꿨으면 아직 흔들리는 중일 수 있다
```

트래픽이 들어오는 순간에 롤아웃이 돌거나 노드가 뜨는 중이면 첫 분부터 5xx 가 나고,
그건 가용성 12점에 직결된다. 그래서 "준비 끝"을 눈짐작이 아니라 이걸로 판정한다.

---

# 4. 시간이 없을 때의 최소 행동

곡선 측정도 못 할 상황이면 아래 다섯 개만 해도 큰 점수를 지킨다.
**트래픽·환경과 무관하게 성립**하는 것들이다.

1. **bastion 등 불필요한 인스턴스 제거** — 비용 2점 + 규정 위반 해소
2. **`minDomains` 설정** — 없으면 Karpenter 가 노드를 아예 안 만든다
3. **`NodePool.spec.limits.cpu` 설정** — 없으면 노드가 무한 증식한다 (실측 9대)
4. **stress `cpu.requests` 를 낮춘다** (`limits` 는 건드리지 마라) — 실측 user +8.5%p
5. **노드 수를 필요 최소로** — 비용은 0.5대당 1점, 성능은 티어당 0.5점. 비용이 두 배 가파르다

## 조심할 것

- **비용 게이트** — 세 앱 중 하나라도 성능 30% 미만이면 **비용 12점이 통째로 0.**
  "노드를 줄여 비용을 번다"는 전략은 이 선을 넘는 순간 역효과다.
  실측: stress 를 7rps 로 올렸더니 동거 구성에서 stress 26% → 40.0 이 **22.5** 로 떨어졌다.
- **stress 전용 노드는 항상 옳지 않다** — stress 가 적으면 동거가 이득(전용 노드 1대 = 비용 2점),
  많으면 격리가 이득이다. 경계는 stress 파드 1개가 **4~5 rps** 에서 포화한다는 점으로 잡는다.
- **stress 에 CPU `limits` 를 걸지 마라** — 조여도 user 는 안 오르고(84.88 → 83.73)
  가용성만 깨진다(product 91.41%). 보호는 `requests` 로 한다.

---

# 5. 어떻게 계산하는가 (원리)

```
채점이 보는 지연  ≈  P + F + d × 배수(동시성)

  P     경로 지연 (채점 서버 ↔ CloudFront ↔ ALB). 실측 상수 ~14.5ms
  F     앱 고정 오버헤드 (DB·직렬화)
  d     요청당 CPU 작업량 분포
  배수  동시성에 따른 지연 증가 — concurrency.sh 가 실측한 곡선에서 읽는다
```

**지연의 대부분은 줄 서는 시간이다.** 요청 하나의 CPU 는 13.6ms 인데 채점이 본 지연은 180ms 였다.

```
동시성  1 → user POST  20.1ms
동시성 20 → user POST 104.2ms      (20 × 13.6ms ÷ 2코어 = 136ms 와 일치)
```

평균 CPU 이용률은 32% 로 한가해 보이는데, 순간 몰림이 대기를 만든다. 그래서 평균 기반
모델(`ρ`, `1/(1-ρ)`)로는 이 지연을 만들 수 없다 — 예측이 전부 99.8% 로 나온다.

> **측정할 때 주의**: 단발 프로브는 큐를 안 만들어 항상 빠르게 나온다.
> 내부 프로브 15.6ms 를 믿고 "지연은 네트워크 탓"이라 결론냈다가
> ALB `TargetResponseTime`(160~180ms)에 반박당했다. **반드시 동시성을 걸어 재라.**

검증 결과와 한계는 `VALIDATION.md`, 만점 구성은 `BEST.md`, 1시간 절차는 `RUNBOOK-1H.md`.

---

# 6. 파일

| 파일 | 역할 |
|---|---|
| `autotune.sh` | **메인.** 준비·감시·조정 전부. 채점 서버 접근 불필요 |
| `pretune.sh` | 트래픽 전 자체 부하로 후보 구성 실측 비교 |
| `pretable.sh` | rps 별 최적 구성 표 생성 |
| `concurrency.sh` | 앱의 동시성-지연 곡선 실측 |
| `solve.py` | 곡선 + 트래픽 → 최적 구성 (비용 게이트 반영) |
| `apply.sh` | 구성 고정 (`minDomains` + `limits.cpu` + NodeClaim 회수) |
| `tune_requests.sh` | stress CPU 지분 조정 |
| `calibrate.py` | 회차 데이터로 계수 재적합 |
| `verify.sh` / `scenario.sh` / `matrix.sh` | 연습 환경 검증용 (대회에선 안 씀) |
| `profile.sh` / `measure_usable.py` / `cpuwatch.sh` | 진단용 (1시간 경로에서는 제외) |
| `VALIDATION.md` | 검증 결과 — 믿을 수 있는 범위와 없는 범위 |
| `BEST.md` | 40.0/40 구성 상세 |
| `RUNBOOK-1H.md` | 1시간 절차 (실측 소요시간) |
