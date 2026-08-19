# 1시간 런북 — 인프라 구축 + 튜닝

대회는 3~4시간이고 **인프라 구축에 주어지는 시간은 1시간**이다. 그 뒤 트래픽이 들어온다.
즉 **트래픽이 오기 전에는 보정 회차를 돌릴 수 없다.** 이 런북은 그 제약에서
"보정 없이 최선"을 세우고, 트래픽이 시작된 뒤 회차 데이터로 다듬는 순서다.

모든 소요시간은 2026-08-19 실측이다.

---

## 0~20분: 인프라 배포

```bash
terraform apply -auto-approve                 # 실측 18분 (EKS 7분 + RDS Multi-AZ 16분 병렬)
```

RDS Multi-AZ 가 가장 오래 걸린다. 이 시간은 못 줄이므로 **가장 먼저 시작**한다.
기다리는 동안 아래 21~30분 항목의 준비(명령어 확인 등)를 해둔다.

## 20~30분: 앱 배포 확인 + bastion 제거

```bash
aws eks update-kubeconfig --region ap-northeast-2 --name apdev-cluster
kubectl -n apdev get pods -o wide             # 3개 앱 Running 확인
kubectl -n apdev get targetgroupbinding       # ALB 연결 확인

terraform apply -var bastion_enabled=false    # 실측 3분
```

bastion 제거는 **점수에 직결**된다(비용 1대 = 2점, t3.small 은 타입 규정 위반).
EKS API 가 퍼블릭이라 이후 제어는 로컬 kubectl 로 한다.

## 30~40분: 동시성 곡선 측정 (이게 최우선 측정이다)

```bash
DUR=8 ./concurrency.sh user post &            # 앱·메서드당 약 1분
DUR=8 ./concurrency.sh user get
DUR=8 ./concurrency.sh product get
DUR=8 ./concurrency.sh stress post
```

곡선 하나가 "이 앱은 2코어로 초당 몇 개까지 처리하는가"를 준다.
**시간이 없으면 `profile.sh` 는 건너뛰어도 된다** — 곡선만으로 노드 수를 고를 수 있다.
(`profile.sh` 는 앱당 5분씩 걸려 1시간 안에 다 넣기 어렵다.)

실측 예 (t3.medium 2코어, 파드 1개):

| 앱 | 포화 처리량 | 동시성 8에서 지연 |
|---|---|---|
| user POST | 82 rps | 47ms |
| user GET | 85 rps | 44ms |
| product GET | 199 rps | 10ms |
| stress POST | **4~5 rps** | 1559ms |

stress 가 다른 앱보다 20배 무겁다는 것이 이 표에서 바로 보인다.

## 40~50분: 초기 구성 결정

트래픽을 아직 모르므로 **과제지에 적힌 SLO 와 곡선만으로** 고른다.

```bash
python3 solve.py --traffic '<예상 트래픽>' --min-nodes 2 --max-nodes 8
./apply.sh <노드수> <배치>
./tune_requests.sh 100m
```

### ★트래픽 규모는 과제지에 없다 — 그래서 사전에 최적값을 확정할 수 없다

과제지는 "경기 시작 1시간 뒤부터 트래픽이 발생"만 말하고 **양은 알려주지 않는다.**
그러니 시작 시점에 할 수 있는 최선은 "안전하게 시작하고, 재고 나서 좁히는 것"이다.

**하한과 상한을 분리한다.**

```bash
./apply.sh 2 shared 6      # 하한 2대, 상한 6대
```

- 하한 2대 (`minDomains`) — 비용 만점 지점이자 고가용성 최소선. 트래픽이 가벼우면 여기 머문다.
- 상한 6대 (`NodePool.limits.cpu`) — 스파이크가 오면 Karpenter 가 늘릴 수 있게 **열어둔다.**

상한까지 2대로 묶으면 예상 못 한 스파이크에 노드를 못 만들고 무너진다.
실측: 그 조건에서 stress 가 26% 로 떨어져 비용 게이트가 터졌고 **22.5점**이었다.

점수는 트래픽 구간 **평균**이다. 초반 몇 분 노드가 한 대 더 도는 비용보다,
성능이 무너져 잃는 점수(성능 12 + 비용 게이트 12)가 훨씬 비싸다.

트래픽이 시작되면 `autotune.sh run` 이 실제 rps 를 재서 최적 구성을 계산하고,
그때 상한을 하한까지 좁혀 비용을 확정한다.

콜드 스타트 기본값:

| 설정 | 값 | 근거 |
|---|---|---|
| 노드 하한 | **2대** | 비용 만점 지점 + 고가용성 최소선 |
| 노드 상한 | **6대** | 스파이크 흡수용. 측정 후 좁힌다 |
| stress 배치 | **동거** | 전용 노드는 비용 2점. stress 가 4rps 를 넘을 때만 격리 |
| stress cpu requests | **100m** | CFS 지분을 user 로 넘긴다 (실측 user +8.5%p) |
| minDomains | 노드 수 | 없으면 Karpenter 가 안 깨어난다 |
| NodePool limits.cpu | 노드 수 × 2 | 없으면 노드가 무한 증식한다 (실측 9대) |

## 50~60분: 예비 확인

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST <엔드포인트>/v1/user \
  -H 'Content-Type: application/json' -H 'User-Agent: Mozilla/5.0' \
  -d '{"requestid":"r","uuid":"u","username":"probe1","email":"probe1@x.com"}'
```

기본 curl User-Agent 는 WAF 에 막혀 403 이 난다. 브라우저 UA 로 확인할 것.
채점 플랫폼 Settings 에 엔드포인트 등록도 이때 끝낸다 (미등록이면 0점).

---

## 트래픽 시작 후: 회차마다 다듬기

첫 회차가 곧 보정 데이터다.

```bash
# 1) 지금 트래픽이 실제로 얼마인지 읽는다 (채점 CSV 또는 ALB 지표)
# 2) 그 값으로 다시 계산
python3 solve.py --traffic '{"user_post":22,"user_get":22,"product_get":45,"product_post":3,"stress":2.5}'
# 3) 적용
./apply.sh 2 shared
```

**감시할 것 두 가지**

1. **비용 게이트** — 세 앱 중 하나라도 성능 30% 미만이면 비용 12점이 통째로 0 이 된다
   (`score_csv.py`: `cost = tier(...) if perf_min >= 30 else 0`).
   실측: stress 를 7rps 로 올렸더니 동거 구성에서 stress 26% → 총점 40.0 → 22.5.
   노드를 줄여 비용을 버는 전략은 이 선을 넘는 순간 역효과다.

2. **stress 포화** — 파드 1개가 4~5rps 에서 포화한다. stress 요청률이 그 근처면
   전용 노드로 격리하고, 전용 노드가 2대 이상 필요하면 `iso2`, `iso3` 를 쓴다.

---

## 시간이 더 없을 때의 최소 행동

곡선 측정도 못 할 상황이면, 아래 다섯 개만 해도 큰 점수를 지킨다.
이것들은 **트래픽·환경과 무관**하게 성립한다.

1. bastion 등 불필요한 인스턴스 제거 (비용 2점, 규정 위반 해소)
2. `minDomains` 설정 (없으면 Karpenter 가 노드를 안 만든다)
3. `NodePool.spec.limits.cpu` 설정 (없으면 노드가 무한 증식한다)
4. stress `cpu.requests` 를 낮춘다 (`limits` 는 건드리지 않는다)
5. 노드 수를 필요 최소로 (비용은 0.5대당 1점, 성능은 티어당 0.5점 — 비용이 두 배 가파르다)
