# 인프라 최적값 튜너

주어진 애플리케이션 바이너리와 트래픽에 대해 **점수가 가장 높은 인프라 구성**(노드 수 +
stress 배치)을 계산하고, 적용하고, 채점으로 검증한다.

앱에 손잡이(디버그 플래그, 부하 조절 환경변수 등)를 요구하지 않는다. 대회에서 주는
바이너리를 그대로 두고 커널이 센 CPU 카운터만 읽는다.

---

## 왜 "성능 최대"가 답이 아닌가

채점 배점(40점): 비정상 4 + 고가용성 12 + 성능 12 + 비용 12

- 성능: 티어 하나당 **0.5점** (앱당 8티어, 90 / 87.5 / 85 / 82.5 / 80 / 70 / 50 / 30 %)
- 비용: `cost_ratio = avg_ec2 / 2` 기준 0.25 구간마다 **1.0점**

**비용이 성능보다 두 배 가파르다.** 노드 0.5대를 아끼면 1점인데, 성능 티어 하나는 0.5점이다.
그래서 최적해는 "성능이 허용하는 선에서 노드를 최대한 줄인 구성"이다.

실측이 그대로 보여준다 (x0.5, 15분 정식 회차):

| 구성 | 성능 | 비용 | 합계 |
|---|---|---|---|
| 노드 5대 | 12.0 | 5.0 | 33.0 |
| 노드 2대 + bastion | 11.0 | 9.0 | 36.0 |

노드를 5대에서 2대로 줄여 성능은 1점 잃고 비용은 4점 벌었다.

---

## 비용 지표의 정체 (반드시 알아야 함)

채점의 `avg_ec2` 는 EKS 노드 수가 아니라 **계정 리전 안의 running EC2 전체 수**다
(채점 서버 `collector.py` 의 `count_ec2`, `describe_instances` 를 필터 없이 센다).

그래서:

- **bastion 도 1대로 잡힌다.** 설치용 bastion 하나가 비용 2점이다.
- 과제 스펙도 같은 방향이다 — "EC2 인스턴스는 **t3.medium 타입만**", "불필요한 리소스를
  생성한 경우 감점 (e.g. **미사용 EC2**)". t3.small bastion 은 두 조항 모두에 걸린다.
- EKS API 가 퍼블릭이면 설치가 끝난 뒤 kubectl 은 로컬에서 그대로 된다. bastion 은 필요 없다.

terraform 에 `bastion_enabled` 를 두었다. 설치가 끝나면:

```bash
terraform apply -var bastion_enabled=false
```

---

## 지연은 어디에 있었나

x0.5 스파이크 중, 같은 시각에 경로를 나눠 측정한 결과:

| 측정 지점 | p50 |
|---|---|
| 파드 (`user-svc` 직접) | 15.6ms |
| ALB 직접 | 21.8ms |
| CloudFront | 27.1ms |
| **채점 서버가 본 값** | **96.8ms** |

내 인프라 안쪽은 전부 27ms 안에 끝나고, **70ms 이상이 채점 서버↔CloudFront 구간**이다.
이 항은 트래픽 양에 비례하고 노드 수와 무관하다 (노드 5대 81.2ms vs 2대 94.4ms).

그래서 모델에 이 항을 명시적으로 넣는다:

```
채점이 보는 지연 ≈ E(R) + F + d / (1 - ρ)

  E(R) : 엣지 지연.  실측 직선 적합 → E(R) = 9.4 + 1.66·R ms
  F    : 고정 오버헤드 (DB·직렬화·클러스터 내 네트워크)
  d    : 요청당 CPU 작업량 분포 (커널 카운터 실측, 무거운 꼬리 포함)
  ρ    : 노드 CPU 이용률
```

> ⚠️ **E(R) 계수를 그대로 대회에 가져가지 마라.** 주입기 위치에 따라 달라지는 값이다.
> `calibrate.py` 가 직접 재도록 만들어 두었으니, 새 환경에서 회차 한 번 돌리고 다시 잡아라.

이 항을 빼면 모델이 통과율을 전부 100% 로 예측하고, 그 오차를 `usable` 을 0.24 까지
낮춰서 억지로 메우게 된다. 실제로 그 상태였고 예측이 전혀 맞지 않았다.

---

## GET 과 POST 는 따로 재야 한다

| 프로파일 | F | d 평균 | d p95 |
|---|---|---|---|
| user POST | 14.84ms | 9.97ms | 33.52ms |
| user GET | 7.55ms | 9.37ms | 36.28ms |
| product POST | 17.60ms | 9.70ms | 35.86ms |
| **product GET** | **1.68ms** | **0.60ms** | **1.02ms** |
| stress POST | 6.26ms | 235.05ms | 543.77ms |

product 는 GET 이 POST 보다 **CPU 16배, 고정비 10배** 싸다. 그런데 트래픽의 94%가 GET 이다.
POST 로만 모델링하면 product 통과율을 88% 로 과소평가한다(실측 100%).
`--traffic` 키에 `product_get` / `product_post` 처럼 메서드를 붙이면 나눠서 계산한다.

---

## usable 계수의 정체

`ρ = 수요 / (노드수 × vCPU × usable)` 의 `usable` 은 **시스템 오버헤드가 아니다.**

`measure_usable.py` 로 직접 재보면:

```
앱 CPU 수요 D   = 0.746 core
노드 실사용량    = 16.1% × 2대 × 2vCPU = 0.644 core
→ 측정된 usable = 1.16   (오버헤드 ≈ 0)
```

**CPU 는 84% 놀고 있다.** 그런데 회차에 맞춘 적합값은 0.20 이다. 이 차이는
평균 이용률로는 안 잡히는 **순간 버스트**(요청 10%가 4~8배 작업, stress 는 건당 235ms)를
평균 ρ 모델이 표현하지 못해 생기는 것이다. 즉 `usable` 은 버스트 흡수 계수로 읽어야 한다.

관련해서 배제된 가설: **t3 CPU 크레딧 스로틀링 아니다.** 크레딧 모드 unlimited,
스파이크 중에도 `CPUCreditBalance` 가 8.5 → 11.7 로 증가, `CPUSurplusCreditBalance` 0.

---

## 쓰는 법

```bash
cp .env.example .env && vi .env          # 채점 서버 주소·비밀번호 (커밋 금지)

# 1) 앱 프로파일 — 앱이 바뀌면 여기부터
./profile.sh user post && ./profile.sh user get
./profile.sh product post && ./profile.sh product get
./profile.sh stress

# 2) 보정 — 채점 환경이 바뀌면 여기부터
python3 calibrate.py

# 3) 최적값 계산 — 트래픽이 바뀌면 --traffic 만
python3 solve.py --traffic '{"user_post":11,"user_get":11,"product_get":22.5,"product_post":1.5,"stress":1.25}'

# 4) 적용 (총 인스턴스 수, stress 배치)
./apply.sh 2 iso

# 5) 채점 회차로 검증
./verify.sh 0.5 15
```

`apply.sh <총노드수> [iso|shared]`

- `iso` — stress 를 taint 된 전용 노드에 격리. CFS 는 requests 비율로 CPU 를 나누는데
  stress 600m : user 70m ≈ 8.6:1 이라 같은 노드면 user 가 굶는다.
- `shared` — 같은 노드에 태워 1대를 아낀다. stress 요청률이 낮을 때만 이득.

어느 쪽이 유리한지는 트래픽에 달렸으므로 `solve.py` 가 둘 다 계산해서 점수로 고른다.

---

## 노드 수를 고정하는 방법: minDomains + limits.cpu

Karpenter 는 **Pending 파드를 봐야** 노드를 만든다. 그런데 스케줄 가능한 노드가 1대뿐이면
topologySpread 의 skew 가 항상 0 이라 파드가 Pending 되지 않고 한 노드에 쌓인다.
그래서 노드가 안 늘어난다.

- `minDomains: N` — 도메인이 N 개 될 때까지 파드를 Pending 시켜 Karpenter 를 미리 깨운다.
  **하한**이다. "부하가 오면 늘린다"(사후)가 아니라 "N대를 항상 유지한다"(사전)가 되므로
  스파이크 시점·크기를 몰라도 동작한다.
- `NodePool.spec.limits.cpu` — **상한**. 안 걸면 HPA 가 파드를 늘리는 만큼 노드가 계속
  붙는다 (실측: x1.0 에서 9대까지 증식 → 비용 0/12).
- `nodeTaintsPolicy: Honor` — stress 전용 노드를 도메인 계산에서 제외한다. 이게 없으면
  그 노드가 "영원히 0개인 도메인"이 되어 maxSkew 를 계속 위반하고 노드가 무한 증식한다.

**축소는 자동으로 안 된다.** `limits.cpu` 를 낮춰도 이미 떠 있는 노드는 안 지워지고,
topologySpread 때문에 Karpenter 의 통합 시뮬레이션도 막힌다(실측: 몇 분을 기다려도 그대로).
`apply.sh` 가 초과분만큼 NodeClaim 을 직접 회수한다.

---

## 측정할 때 빠졌던 함정

1. **종료 중인 파드를 골라 CPU 를 0 으로 읽음** — replicas 를 줄인 직후 `items[0]` 가
   Terminating 파드를 집는다. `--field-selector=status.phase=Running` 으로 1개가 될 때까지 기다린다.
2. **cAdvisor 갱신 주기(10~15초)** — 1.4초짜리 측정은 실제 CPU 의 23% 만 잡았다.
   샘플 800회 + 두 번째 읽기 전 25초 대기로 해결.
3. **kubelet 메트릭의 마지막 필드는 타임스탬프** — 값은 `$(NF-1)`.
4. **채점 서버 접속 실패가 조용히 넘어감** — `gx` 가 stderr 를 버리고 `run_wait` 가
   타임아웃에도 성공을 반환해서, 부하가 한 건도 안 들어온 회차의 점수를 그대로 믿었다.
   지금은 3회 재시도 후 실패로 끝내고, 회차 진행이 멈추면 중단한다.
5. **채점 회차는 선수별 키로 시작해야 함** — `admin.py start` 는 전역 `injection_running`
   만 켠다. 엔진은 `injection_running:<선수>` 를 읽는다.
6. **`ec2_offset`** — 계정에 과제와 무관한 인스턴스가 있으면 그 수만큼 설정해야 비용이 맞다.
7. **healthcheck 가 섞인 표본** — ALB healthcheck 요청이 지연 분포에 들어가면 가짜 결론이 난다.

---

## 파일

| 파일 | 역할 |
|---|---|
| `profile.sh <app> [post\|get]` | 앱의 F 와 d 분포를 커널 카운터로 실측 |
| `measure_usable.py` | CloudWatch 사용률로 usable 을 직접 측정 |
| `calibrate.py` | 엣지 직선 적합 + usable 격자 적합 |
| `solve.py` | 노드 수 × stress 배치 전수 탐색 → 최고 점수 구성 |
| `apply.sh <노드수> [iso\|shared]` | minDomains + limits.cpu + NodeClaim 회수로 구성 고정 |
| `verify.sh <배수> [분]` | 채점 회차 실행 + 점수 출력 |
| `cpuwatch.sh [초]` | 회차 중 앱별 누적 CPU·파드 수 기록 |
| `sweep.sh <배수> <후보...>` | 후보 구성들을 순회하며 실측 비교 |
| `observations.json` | 회차 관측 + 엣지 표본 (보정 입력) |
| `calibration.json` | 보정 결과 |
