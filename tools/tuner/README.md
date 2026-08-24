# 인프라 튜너 — 처음 쓰는 사람용

> ## ⚠ 시작하기 전에 딱 3가지
>
> 1. **DB 커넥션 한도 올리기 (terraform 에 없다 — 까먹으면 피크에서 연결 거부)**
>    ```bash
>    aws rds modify-db-parameter-group --db-parameter-group-name apdev-mysql8 \
>      --parameters "ParameterName=max_connections,ParameterValue=150,ApplyMethod=immediate"
>    ```
> 2. **`./GO.sh doctor` 에 [X] 가 하나라도 있으면 트래픽 시작 금지.**
>    특히 낯선 EC2(4f) — 대회에선 채점에 그대로 세어진다. 진짜 꺼라.
> 3. **`./GO.sh watch` 는 회차 시작 때 정확히 한 번만.** 켠 뒤 다른 터미널에서
>    `./GO.sh monitor` 를 띄워 "튜너 [O] 판단 중" 인지 확인해라 —
>    죽은 튜너를 120분간 모르고 지나간 사고가 실제로 있었다.

이 도구는 **대회 중에 "노드를 몇 대 띄우고 앱을 어떻게 배치할지"를 대신 정해준다.**
직접 부하를 넣어 재보고, 트래픽이 들어오면 실시간으로 따라간다.

실적:
- 연습 x0.5 (15분): **40.0/40** — 문서 경로 그대로 2회 재현
- **공식 120분 곡선: 30.0/40** (2026-08-25) — 게이트 통과, 가용성 만점, 평균 3.77대
  (대조군 30.0 은 3.87대였다 — 같은 점수를 더 싸게 낸다)

---

# 시작하기 전에 딱 세 가지만

**1. 이 도구가 정하는 것**

- EC2 노드를 몇 대 띄울지
- `stress` 앱을 다른 앱과 같은 노드에 둘지, 따로 뗄지
- `stress` 의 CPU 몫을 얼마로 줄지

**2. 이 도구가 안 건드리는 것**

인스턴스 타입(t3.medium), DB(db.t3.micro Multi-AZ), 앱 바이너리. 과제 제약이라 손대면 안 된다.

**3. 준비물**

```bash
kubectl   # 클러스터에 붙어 있어야 함
aws       # AWS CLI (프로파일 설정 완료)
python3
```

`terraform apply` 가 끝나 있어야 한다.

---

# 대회 당일 — 이대로만 하면 된다

## ① 인프라 배포 (0~20분)

```bash
cd terraform
terraform apply -auto-approve
```

RDS 가 16분쯤 걸린다. **제일 먼저 시작해두고** 다른 준비를 하자.

## ② bastion 제거 + kubectl 연결 (20~25분)

```bash
aws eks update-kubeconfig --region ap-northeast-2 --name apdev-cluster
kubectl -n apdev get pods                     # user, product, stress 가 Running 이면 OK

terraform apply -var bastion_enabled=false
cd ../tools/tuner
```

> **왜 bastion 을 지우나?**
> 채점의 비용 점수는 "EKS 노드 수"가 아니라 **계정에 켜져 있는 EC2 전체 수**를 센다.
> 설치용으로 만든 bastion 한 대가 **비용 2점**을 깎아먹는다.
> 게다가 t3.small 이라 "EC2 는 t3.medium 만" 규정도 어긴다.
> 설치가 끝나면 필요 없다 — EKS API 가 공개돼 있어서 내 노트북에서 kubectl 로 다 된다.

## ③ 튜너 준비 (25~55분)

```bash
export AWS_PROFILE=<본인 프로파일>
./GO.sh setup
```

**이 한 줄이 다음을 순서대로 한다:**

1. 클러스터 상태 정리 (남은 잠금·이전 흔적 제거)
2. **2대 동거**로 출발 구성 고정 + stress cpu.requests 100m
3. HPA 상한 적용 — user/product 35 (파드 수가 처리량을 정한다), stress 6
4. `doctor` 로 "트래픽 받아도 되는 상태"인지 검사 (권한·타깃·분산·컨트롤러 등)

5~15분 걸린다. 사전 부하 탐색은 일부러 뺐다 — 낮게 시작해 실측으로 올리는
쪽이 언제나 싸다 (비용은 분 평균이다).

**그리고 DB 파라미터 하나를 수동으로 올려라 (terraform 에 없다):**

```bash
aws rds modify-db-parameter-group --db-parameter-group-name apdev-mysql8 \
  --parameters "ParameterName=max_connections,ParameterValue=150,ApplyMethod=immediate"
```

> **왜?** 파드 상한을 35 로 열면 피크에서 DB 커넥션이 100개 근처까지 간다.
> db.t3.micro 기본 한도는 약 85 — 안 올리면 피크 한복판에서 연결이 거부된다.
> 동적 파라미터라 재시작 없이 즉시 적용된다. (실측: 피크 커넥션 99, 한도 150 안)

## ④ 엔드포인트 등록 (55~60분)

채점 플랫폼 Settings 에 CloudFront 주소를 넣는다. **안 넣으면 0점이다.**

```bash
terraform -chdir=../../terraform output endpoint     # 주소 확인
```

넣고 나서 잘 되는지 확인:

```bash
curl -o /dev/null -w '%{http_code}\n' -X POST <주소>/v1/user \
  -H 'Content-Type: application/json' -H 'User-Agent: Mozilla/5.0' \
  -d '{"requestid":"r","uuid":"u","username":"probe1","email":"probe1@x.com"}'
```

`201` 이 나오면 정상.

> **403 이 나오면 body 를 확인해라.** WAF 는 기본 동작이 차단이고, 허용 규칙에 맞는 요청만
> 통과시킨다. `POST /v1/user` 는 body 에 `username` 이 있어야 하고, `product` 는 `name`·`price`,
> `stress` 는 `length` 가 있어야 한다. 필드를 빠뜨리면 앱에 닿기도 전에 403 이다.
> (헤더 검사는 SQL 인젝션·XSS·스캐너 이름 같은 공격 시그니처만 본다. User-Agent 유무는 상관없다.)
>
> 정상 트래픽은 막히지 않는다 — 채점 주입기는 필수 필드를 다 갖춰 보낸다.
> 막히는 건 채점이 일부러 보내는 비정상 요청이고, 그게 '비정상 처리 4점' 항목이다.

## ⑤ 트래픽 직전 — 마지막 점검 후 watch

```bash
./GO.sh doctor    # ★[X] 가 하나라도 있으면 트래픽 받지 마라. 고치고 다시.
./GO.sh watch
```

> doctor 가 "워커가 아닌 EC2" 를 잡으면 **진짜로 꺼라** — 계정의 running EC2
> 전부가 비용으로 세어진다. (연습 환경에서만 wsi-* 는 예외였다.)

watch 는 백그라운드로 돌면서 알아서 조정한다. **여기까지 하면 사람이 할 일은 끝이다.**

궁금하면 `./GO.sh status` 로 상태를 본다.

---

# 자주 하는 질문

**Q. `GO.sh` 가 알려주는 점수는 채점 점수인가?**

아니다. 채점 공식을 똑같이 써서 **자체 부하로 매긴 값**이다. 구성끼리 순위를 비교하는 용도다.
실제 점수는 채점 회차를 돌려야 안다(`practice/verify.sh`, 사내 채점 서버 전용).

**Q. 트래픽이 갑자기 늘면?**

세 가지가 알아서 움직인다.

| | 반응 속도 | 하는 일 |
|---|---|---|
| HPA + Karpenter | 1분 이내 | 파드·노드를 자동으로 늘림 |
| `GO.sh watch` | 1분 | 응답이 SLA 를 넘으면 노드 상한을 즉시 올림 |
| `GO.sh watch` | 10분마다 | 구성 자체를 다시 계산 |

**Q. 노드가 안 줄어드는데?**

Karpenter 는 **늘리는 건 알아서 하지만 줄이는 건 안 한다.** `apply.sh` 가 대신 회수한다.

```bash
./apply.sh 2 shared 2      # 하한 2대, 상한 2대 → 초과분 회수
```

**Q. 뭔가 이상한데 어디부터 보나?**

```bash
./GO.sh status             # 노드·파드·트래픽·안정화 한 눈에
kubectl -n apdev get pods  # Pending 이 있으면 노드가 모자란 것
tail -30 autotune.log      # watch 로그
```

**Q. 결과에 "표본부족" 이라고 나온다.**

`stress` 는 전체 트래픽의 3% 뿐이라, 부하 시간이 짧으면 판정에 필요한 150건을 못 채운다.
그런 구성은 **점수 계산에서 빠진다**(틀린 점수를 내느니 빼는 게 낫다).

부하 시간을 늘려서 다시 재면 된다:

```bash
DUR=240 ./pretune.sh
```

필요 시간 ≈ `150 ÷ (달성rps × 0.03)`. 달성 rps 는 결과표 오른쪽에 찍힌다.

**Q. `bad interpreter: /usr/bin/env bash^M` 이라고 나온다.**

Windows 에서 클론해서 줄바꿈이 CRLF 로 바뀐 것이다. Git for Windows 는
`core.autocrlf=true` 가 기본이라 체크아웃할 때 LF 를 CRLF 로 바꾼다.

`./GO.sh` 는 이걸 감지해서 자동으로 고친다. 다만 **`GO.sh` 자기 자신이 CRLF 면
실행조차 안 되므로** 그때는 수동으로 한 번 돌려라:

```bash
for f in *.sh *.py practice/*.sh; do tr -d '\r' < "$f" > "$f.lf" && mv "$f.lf" "$f"; done
chmod +x *.sh practice/*.sh
```

재발 방지(한 번만):

```bash
git config --global core.autocrlf input
```

저장소에 `.gitattributes` 로 LF 를 강제해뒀으니, 새로 클론하면 이 문제는 안 생긴다.

**Q. 처음 상태로 되돌리려면?**

```bash
./apply.sh 2 shared 6      # 하한 2대 / 상한 6대 (기본 시작 구성)
./tune_requests.sh 100m    # stress CPU 몫 기본값
```

---

# 손대면 안 되는 것

- **`pretune.sh` 의 `W_STRESS`** — 채점 주입기와 같은 비율(3%)이어야 한다.
  표본을 늘리려고 12% 로 올렸다가 판단이 뒤집힌 적이 있다
  (정답 2노드 40.0 대신 4노드 33.5 를 골랐다). 표본은 비율이 아니라 시간(`DUR`)으로 늘려라.
- **`stress` 의 `limits.cpu`** — 조여도 user 는 안 좋아지고 가용성만 깨진다.
  CPU 몫 조절은 `requests` 로 한다(`tune_requests.sh`).
- **인스턴스 타입 / DB 스펙 / 앱 바이너리** — 과제 제약.

---

# 알고 있으면 좋은 것

## 비용이 성능보다 두 배 가파르다

```
비용:  노드 0.5대 줄일 때마다  +1.0점
성능:  티어 하나 올릴 때마다   +0.5점
```

그래서 정답은 항상 **"성능이 버티는 선에서 노드를 최대한 줄인 구성"** 이다.

## 단, 넘으면 안 되는 선이 있다

세 앱 중 **하나라도 성능이 30% 밑으로 가면 비용 12점이 통째로 0** 이 된다.

실제로 겪은 일: stress 트래픽이 늘었는데 노드를 안 늘려서 stress 가 26% 로 떨어졌고,
**40.0 이던 점수가 22.5 로 폭락**했다. 노드를 아끼려다 12점을 날린 것이다.

## stress 를 따로 뗄지 말지

| stress 요청률 | 배치 | 왜 |
|---|---|---|
| ~2 rps | 같은 노드 (`shared`) | 전용 노드 1대 = 비용 2점인데 그만큼 안 쓴다 |
| 2~4 rps | 전용 1대 (`iso`) | |
| 4~9 rps | 전용 2대 (`iso2`) | 파드 1개가 4~5rps 에서 한계 |
| 그 이상 | 전용 3대 (`iso3`) | |

실측으로 정확히 갈렸다:

```
stress 1.25rps →  같은 노드 40.0  /  전용 37.0
stress 7rps    →  같은 노드 22.5  /  전용 2대 32.0
```

`stress` 는 요청 하나가 다른 앱의 **20배** 무겁다. 파드당 처리 한계가
user 82rps, product 199rps 인데 stress 는 4~5rps 다.

## 지연은 대부분 "줄 서는 시간"이다

요청 하나가 실제로 쓰는 CPU 는 13.6ms 인데 채점이 본 지연은 180ms 였다.

```
동시에 1개 요청  →  20ms
동시에 20개 요청 → 104ms      (20 × 13.6ms ÷ 2코어 = 136ms)
```

**그래서 코어 수(=노드 수)가 곧 성능이다.** 평균 CPU 사용률은 32% 로 한가해 보여도,
순간에 몰리면 줄이 생긴다.

> 측정할 때 주의: 요청을 하나씩 보내면 줄이 안 생겨서 항상 빠르게 나온다.
> 그거 믿고 "네트워크가 느린 것 같다"고 결론냈다가 ALB 지표에 반박당했다.
> **꼭 동시에 여러 개를 보내서 재라.**

---

# 이 도구의 한계 (반드시 알고 쓸 것)

**1. 시스템이 무너지는 상황은 예측 못 한다.**

앱을 2배 무겁게 했더니 예측 35.5점, 실제 16.5점이었다. 응답이 12초까지 늘어 요청이
타임아웃되는 상황을 모델이 모른다. 그래서 예측과 별개로 **"ALB 응답이 SLA 를 넘으면
무조건 노드를 늘린다"** 는 안전장치를 넣었다.

**2. 환경이 바뀌면 숫자를 다시 잡아야 한다.**

`calibration.json` 의 값들은 이 연습 환경에서 잰 것이다. 대회장은 네트워크도 주입기도
다르다. 트래픽이 시작되면 `GO.sh watch` 가 실측으로 자동 보정하지만, **트래픽 전 예측은
그만큼 덜 정확하다.**

**3. 긴 회차 검증 완료 (2026-08-24~25).**

공식 120분 곡선(8구간, peak2 311rps 25분 포함)을 세 번 완주했다: 28.0 → 28.0 → **30.0**.
진동 없이 상승·하강·재상승을 전부 따라갔고, 마지막 회차는 `setup → doctor → watch`
문서 경로 그대로 무인 자동으로 낸 숫자다.

남은 구조적 한계: **peak 진입 후 3~5분의 회복 구간**. ALB 지표가 완결된 직전
1분만 보이는 관측 지연 + 노드 부팅 시간이라, 곡선을 미리 알지 않는 한 못 없앤다.
user perf 가 45~48% 에 머무는 이유가 이것이다.

자세한 검증 내용과 한계는 `VALIDATION.md` 에 있다.

---

# 파일 안내

| 파일 | 언제 보나 |
|---|---|
| **`GO.sh`** | **이것만 쓰면 된다** |
| `VALIDATION.md` | 이 도구를 믿어도 되는 범위 |
| `BEST.md` | 40점 구성이 정확히 뭐였는지 |
| `RUNBOOK-1H.md` | 1시간 절차 상세 (실측 소요시간) |

내부 도구 (`GO.sh` 가 알아서 부른다):

| 파일 | 역할 |
|---|---|
| `autotune.sh` | 준비·감시·조정 |
| `pretune.sh` | 자체 부하로 구성 비교 |
| `concurrency.sh` | 앱 처리 한계 측정 |
| `solve.py` | 최적 구성 계산 |
| `apply.sh` | 구성 적용 |
| `tune_requests.sh` | stress CPU 몫 조절 |
| `common.sh` | 클러스터/AWS 공통 헬퍼 |

연습 전용 (`practice/`) — **대회에서는 안 쓴다. 사내 채점 서버가 있어야 돈다:**

| 파일 | 역할 |
|---|---|
| `practice/verify.sh` | 채점 회차 실행 (`practice/.env` 필요) |
| `practice/scenario.sh` | 연습용 트래픽 모양 변경 |
| `practice/grader.sh` | 채점 서버 제어 함수 |

## 판단 기준 — 왜 "노드를 늘려서 이기려는 전략"은 지는가

채점표(`score.py` 가 그대로 옮겨놨다)에서 나오는 산수다.

| 항목 | 만점 조건 | 한 칸의 값 |
|---|---|---|
| 비용 12점 | 회차 **분 평균** 노드 2.00대 이하 | 0.5대마다 1점 → **노드 1대 = 2점** |
| 성능 12점 | 세 앱 모두 SLA 안에 든 **요청** 90% 이상 | 앱당 최대 4점 |
| 고가용성 12점 | 세 앱 모두 2xx 90% 이상 | 앱당 최대 4점 |

여기서 두 가지가 따라 나온다.

**1. 노드 1대는 성능 반 앱 값이다.** 한 대 더 사서 한 앱을 0% → 100% 로 올려야
겨우 본전이다. 그래서 기본자세는 **2대 동거(shared)** 다. stress 를 전용 노드로
빼는 순간 최소 3대 = 비용 10점이 천장이 된다.

**2. 비용은 '분' 평균인데 성능은 '요청' 비율이다.** 트래픽이 없는 1분과 피크 1분의
비용이 같다. 계곡에서 노드를 켜두는 건 점수를 그냥 버리는 것이다.
실측 대조군은 하강 구간 40분을 평균 4.93대로 버텼다.

그리고 비용이 분 평균이라는 사실에서 하나가 더 나온다 —
**매 분 제약을 만족하는 최소 노드 수를 쓰면 회차 길이와 무관하게 평균이 최소가 된다.**
그래서 이 도구는 **회차 길이를 묻지 않는다.** 15분 x0.5 든 공식 120분이든 같은
판단으로 돈다. 따로 맞출 게 없다.

### 도는 방식

```
alb_snapshot.sh   ALB 에서 앱별 요청수·5xx·지연 백분위(p10~p99)를 한 번에 읽는다
      ↓           ★평균이 아니라 백분위다. 채점되는 값이 'SLA 안에 든 비율'이라
      ↓             SLA 가 분포 어디에 놓였는지를 봐야 tier 를 겨냥할 수 있다.
decide.py         점수 산수로 +1 / 0 / -1 을 낸다
      ↓
autotune.sh       그걸 배치(공유 / stress 전용)로 옮겨 apply.sh 를 부른다
```

`decide.py` 의 우선순위:

1. **가용성 방어** — 5xx 가 보이면 즉시 증설. 이미 흘린 요청은 되돌릴 수 없다.
2. **비용 게이트 방어** — 누적 통과율이 30% 밑이면 비용 12점이 통째로 0 이다.
3. **목표 추격** — 90% tier 를 못 넘긴 앱이 있으면 **한 대만** 늘리고 효과를 본다.
   못 움직이면 그 회차에서는 더 안 늘린다. 트래픽이 1.5배로 커지면 다시 시험한다.
4. **축소** — 전 앱이 여유선(95%) 위면 한 대 반납한다. 상한도 같이 닫는다
   (안 닫으면 `apply.sh` 가 NodeClaim 을 회수하지 않아 노드가 안 줄어든다).

### 검증

```bash
./GO.sh check     # 판단 로직 + 배치 변환. AWS·클러스터 불필요, 수 초, 무료
./GO.sh score     # 회차 중 아무 때나 누적 점수 전망
```

실측 회차 한 번은 38분 + 인프라 비용이다. 분기 검증은 `check` 에서 끝낸다.

## 환경과 준비물

대회 PC 는 Windows + WSL 이다. **2026-08-21 에 실제 WSL2 에서 검증했다** —
`microsoft-standard-WSL2`, bash 5.2.21, python 3.12.3, `timeout` 있음, `mapfile` 빌트인.
오프라인 검증 전부 통과, `GO.sh doctor` 7개 항목 전부 통과, probe 2.5초.

### 튜너 본체는 추가 설치가 필요 없다

`GO.sh` · `autotune.sh` · `decide.py` · `score.py` · `probe.sh` · `alb_snapshot.sh` ·
`apply.sh` · `doctor.sh` 는 표준 라이브러리와 아래 두 개만 쓴다.

| 필요한 것 | 확인 |
|---|---|
| `aws` CLI v2 | `aws sts get-caller-identity` |
| `kubectl` | `kubectl get nodes` |
| `python3` (표준 라이브러리만) | `python3 -V` |
| `curl` | probe 가 쓴다 |

### 연습용 부하 발생기만 aiohttp 가 필요하다

`practice/loadcurve.py` (채점기 없이 회차를 돌리고 채점하는 도구) 하나만
`aiohttp` 를 쓴다. 없으면 이것만 못 돌고 튜너는 멀쩡히 돈다.

실제 대회 PC(WSL2, Ubuntu, python 3.12)에서 통한 명령은 이것이다:

```bash
sudo apt-get install -y python3-pip
python3 -m pip install --user --break-system-packages aiohttp
```

> 세 가지가 다 필요했다:
> · `pip3` 실행파일이 없다 → `python3 -m pip` 로 부른다
> · `python3 -m pip` 자체가 없다 → `apt-get install python3-pip` 먼저
> · python 3.12 는 외부 관리 환경이라 그냥 설치하면 거부한다(PEP 668)
>   → `--break-system-packages` (사용자 홈에만 설치되므로 시스템은 안 건드린다)

### macOS 에서도 돌지만 주의

개발·검증용으로 macOS 에서도 돈다. 다만 기본 bash 가 3.2 이고 `timeout` 이 없어서,
도구 안에서 각각 우회하고 있다(`probe.sh` 는 `mapfile` 대신 read 루프,
`common.sh` 는 `timeout` 대신 perl). **대회 실행 환경은 WSL 이 기준이다.**
