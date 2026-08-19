# 🔴🔴🔴 시작 전 반드시 할 것 🔴🔴🔴

## ⚠️ 0. 환경변수 — 앱/dump 바꾸면 **제일 먼저** 확인! ⚠️

> **앱 3개는 DB 접속에 환경변수 5개를 씀** (`terraform/k8s/configmap.yaml`, terraform이 자동 주입):
> `MYSQL_USER` · `MYSQL_PASSWORD` · `MYSQL_HOST` · `MYSQL_PORT` · `MYSQL_DBNAME`(=`dev`)
>
> **🚨 앱 바이너리나 dump를 바꾸면 = 이 5개가 새 앱/DB와 맞는지 반드시 확인.**
> - `MYSQL_DBNAME`이 dump가 만드는 DB명과 다르면 → **앱이 DB 접속 실패 → 파드 전부 죽음 → 전멸(0점)**
> - 새 앱이 **다른 env 이름/추가 변수**를 요구하면 → `configmap.yaml`에 반영
> - 확인법: 배포 후 `kubectl get pods -n apdev` 가 전부 **Running**(CrashLoop 아님)이면 접속 OK
>
> **➡ 앱/dump 교체 = env 체크는 세트다. 잊으면 전멸.**

---

## 🟢 점수 직결 5가지 (2026-08-19 실측, 33.0 → 40.0)

측정 없이도 그냥 적용하면 되는 것들이다. 상세 근거는 `DESIGN.md` 부록 A 와
`tools/tuner/VALIDATION.md` 에 있다.

1. **설치가 끝나면 bastion 을 없앤다** — `terraform apply -var bastion_enabled=false`
   비용 지표는 EKS 노드가 아니라 **계정의 running EC2 전체 수**다(채점 `collector.py`).
   bastion 한 대가 **비용 2점**이고, t3.small 이라 "t3.medium 타입만" 규정도 어긴다.
   EKS API 가 퍼블릭이라 이후 kubectl 은 로컬에서 된다. (실측 36.0 → 38.5)

2. **`minDomains` 를 건다** — 없으면 Karpenter 가 노드를 아예 안 만든다.
   스케줄 가능한 노드가 1대뿐이면 `topologySpread` 의 skew 가 항상 0 이라 파드가
   Pending 되지 않고, Karpenter 는 Pending 을 봐야 움직인다.

3. **`NodePool.spec.limits.cpu` 로 상한을 건다** — 없으면 HPA 가 늘리는 만큼 노드가
   계속 붙는다 (**실측 9대**, 비용 0점).

4. **노드를 줄일 때는 NodeClaim 을 직접 지운다** — Karpenter 는 늘리는 건 알아서 하지만
   **줄이는 건 절대 알아서 안 한다.** `limits.cpu` 를 낮춰도 이미 뜬 노드는 그대로다.
   공유 풀과 stress 풀 **둘 다** 해당한다. (`tools/tuner/apply.sh` 가 처리)

5. **stress 의 `cpu.requests` 를 낮춘다** (`limits` 는 건드리지 않는다)
   CFS 는 경합 시 CPU 를 `requests` 비율로 나눈다. 기본은 stress 600m : user 70m = 8.6:1.
   100m 로 낮추면 user 가 +8.5%p 오르고 stress 는 −2.3%p 만 내려간다. (실측 38.5 → 40.0)
   단, stress 가 90% 티어를 깨면 이득이 사라지므로 두 값을 같이 봐야 한다.

### 그리고 조심할 것

- **비용 점수에는 성능 게이트가 있다.** 세 앱 중 하나라도 성능 30% 미만이면 **비용 12점이 통째로 0.**
  "노드를 줄여 비용을 번다"는 전략은 이 선을 넘는 순간 역효과다.
  실측: stress 를 7rps 로 올렸더니 동거 구성에서 stress 26% → 40.0 이 **22.5** 로 떨어졌다.
- **stress 전용 노드는 항상 옳지 않다.** stress 가 적으면 동거가 이득(전용 노드 1대 = 비용 2점),
  많으면 격리가 이득이다. 경계는 stress 파드 1개가 **4~5 rps** 에서 포화한다는 점으로 잡는다.

### 1시간 안에 끝내야 한다면

`tools/tuner/RUNBOOK-1H.md` 를 따른다. 핵심은 `profile.sh`(앱당 5분)를 버리고
`concurrency.sh`(앱당 1분)만 쓰는 것, 그리고 트래픽이 시작되면
`tools/tuner/autotune.sh run` 을 백그라운드로 돌려두는 것이다.
`autotune.sh` 는 ALB 지표로 트래픽을 읽으므로 채점 서버 접근이 필요 없다.


---

## 1. `terraform apply` 전에 4개 파일을 대회용으로 교체

| 교체할 파일 | 위치 |
|---|---|
| **user 바이너리** | `terraform/application/user/user` |
| **product 바이너리** | `terraform/application/product/product` |
| **stress 바이너리** | `terraform/application/stress/stress` |
| **DB 덤프** | `terraform/application/load_user.dump` |

> 이걸 안 바꾸고 apply하면 연습용 앱/데이터로 배포됩니다. **apply 전에 무조건 교체.**

## 2. 절대 하지 말 것

- ❌ **노드는 `t3.medium`만** 사용 (다른 타입 = 감점)
- ❌ **삽입한 DB 데이터 임의 수정/삭제 금지** (임의 데이터 삽입 시 성능 저하 = 감점)
- ❌ **채점 플랫폼 엔드포인트는 프로토콜+주소만** — 경로 X
  - ✅ `https://example.org`   ❌ `example.org`   ❌ `https://example.org/v1/`
- ⚠ **`update_waf.py`(헤더 이름 화이트리스트)는 기본 미사용** — 헤더 이름 화이트리스트는 '미래의 정상 헤더'라는 잔여원. 정상차단 0을 원하면 **켜지 말 것**(base waf.tf만으로 정상 100% 통과 + 비정상 차단 완결). 굳이 켰다 정상 막히면 `--remove`
- ❌ DB 스펙 고정: identifier `apdev-rds-instance`, `db.t3.micro`, Multi-AZ

## 3. 스펙이 바뀌면? (종이로 API/필드 변경 공지 시) — 쉽게

**원칙: 안 바뀌면 아무것도 안 건드림. 바뀐 것만 아래 표대로 맞추면 끝.**

| 바뀐 것 | 고칠 곳 (이것만) |
|---|---|
| **필드 추가/이름변경** (바디·쿼리) | ① `tools/turn.py` 요청(L148~165) ② DB관련이면 `configmap.yaml` env ③ 형식검증 필드면 `waf.tf` regex(L35~39) |
| **형식 변경** (id 숫자만 등) | `waf.tf` regex(L35~39) + turn.py seed값 |
| **새 경로 추가** (/v1/xxx) | `waf.tf` locals(L26) + `alb.tf` 라우팅 + turn.py |
| **앱/dump 교체** | 위 **§0 환경변수 체크** (제일 중요) |

**🚨 급하면 (정상이 막히는데 시간 없음):** `terraform/waf.tf` **L106** 딱 한 줄
`default_action { block {} }` → **`allow {}`** 로 바꾸고 apply
→ 정상 무조건 통과(가용성 방어), 공격은 시그니처+404로 계속 차단. **만점보다 가용성 먼저.**

> 각 변경의 정확한 위치·복붙 스니펫은 **`SPEC_CHANGE_PLAYBOOK.md`** 참조. 위 표로 대부분 커버됨.

---
---

# WSC2026 Day3 — 클라우드컴퓨팅 3과제

## 아키텍처

```
CloudFront (WAF + product 캐싱) → ALB → EKS (HPA + Karpenter + scaler) → RDS Proxy → RDS MySQL
                                                    ↑
                                              S3 (images)
```

- **MNG 2대(t3.medium) 고정** + Karpenter 오토스케일(하드캡)
- 스케일: **HPA(CPU) + Karpenter(노드)** 자립형 — turn.py가 준비시간에 SLO 지키는 최소비용 지점으로 보정. (scaler.py는 선택적 보조, 필수 아님)
- **WAF**: 잔여 0 화이트리스트(default BLOCK) — **메서드+경로**만 검사(정상은 정의상 항상 만족 → 절대 안 막힘) + 공격 블랙리스트(query/body/헤더값) / 없는 경로 404 / 공격·잘못된 메서드 403
- **캐싱**: product GET을 CloudFront 엣지에서 (TTL 1시간)

---

## 순서 (이대로만 하면 됨)

### ⓪ 사전 준비 (내 PC에 최초 1회 설치)

**필수 CLI 4개**
| 도구 | 용도 | 설치 |
|---|---|---|
| **Choco Install** | 패키지 관리 |  Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager ::SecurityProtocol -bor 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1')) |
| **Terraform** | 인프라 배포 | https://developer.hashicorp.com/terraform/install |
| **AWS CLI v2** | terraform/kubectl 인증(EKS 토큰), kubeconfig | https://awscli.amazonaws.com/AWSCLIV2.msi |
| **kubectl** | 모든 파이썬 툴이 클러스터 제어 | choco install kubernetes-cli -y |
| **Python 3.9+ & pip** | 튜닝/WAF/모니터 툴 | https://www.python.org/downloads/ |

> eksctl·helm은 **bastion EC2가 자동 설치**(Karpenter용)라 내 PC엔 불필요.

**1) AWS 자격증명 설정** (한 번)
```bash
aws configure          # Access Key / Secret / region(예: ap-northeast-2) 입력
aws sts get-caller-identity   # 신원 뜨면 OK
```

**2) 파이썬 패키지 설치**
```bash
cd wsc2026-Day3
pip install -r requirements.txt      # aiohttp, boto3 (+dashboard용 flask)
```
> 핵심은 **aiohttp**(turn.py), **boto3**(update_waf.py). scaler.py는 표준 라이브러리라 설치 불필요.

**3) kubectl을 클러스터에 연결** (apply 완료 후)
```bash
aws eks update-kubeconfig --name apdev-cluster --region <배포한 region>
kubectl get nodes        # t3.medium 노드 뜨면 연결 OK
```
> ✅ **로컬 접근은 자동 설정됨** — terraform이 "apply를 실행한 IAM 신원"에 클러스터 관리자 access entry를 만들어 둠(eks.tf `caller`). 그래서 **aws-auth 손댈 필요 없음**.
> ⚠ **조건**: kubectl 돌리는 PC의 `aws configure` 신원 = **terraform apply 돌린 신원**이어야 함(보통 같은 PC면 자동으로 동일).
> ⚠ **bastion 삭제 시**: bastion은 별도 access entry라 지워도 로컬 접근에 영향 없음. 단, **지우기 전에 로컬에서 `kubectl get nodes`가 되는지 꼭 확인**하고 삭제할 것.

---

### ① Terraform apply (인프라 + 자동 배포)

```bash
cd terraform
terraform init
terraform apply \
  -var="node_instance_type=t3.medium" \
  -var="db_instance_class=db.t3.micro" \
  -var="db_allocated_storage=500"
```

> `db_allocated_storage`: 연습은 200, **대회는 500+** (IOPS/처리량 확보). apply는 bastion EC2가 자동으로 DB 덤프 로드 + ECR 빌드/푸시 + k8s 배포까지 함.

```bash
terraform output endpoint   # → CloudFront 엔드포인트 (이걸 채점 플랫폼에 입력)
```

### ② 배포 완료 확인

```bash
# bastion EC2 접속 (Session Manager) 후
tail -f /home/ec2-user/setup.log
# "=== SETUP COMPLETE ===" 나올 때까지 대기

# 파드 확인
kubectl get pods -n apdev     # user/product/stress 전부 Running
kubectl get nodes             # t3.medium 2대 Ready
```

### ③ WAF + 캐싱 검증 (curl)

```bash
EP=<CloudFront endpoint>

# 정상 GET → 통과 (200)
curl -s -o /dev/null -w "%{http_code}\n" "$EP/v1/product?id=dbdump500001&requestid=1&uuid=1"

# product GET 2번 → 2번째 X-Cache: Hit 확인 (캐싱 작동)
curl -sI "$EP/v1/product?id=dbdump500001&requestid=1&uuid=1" | grep -i x-cache
curl -sI "$EP/v1/product?id=dbdump500001&requestid=1&uuid=1" | grep -i x-cache

# 없는 경로 → 404
curl -s -o /dev/null -w "%{http_code}\n" "$EP/v1/none"

# 공격 패턴 → 403
curl -s -o /dev/null -w "%{http_code}\n" "$EP/v1/product?id=1%20union%20select%201"
```

### ④ 튜닝 (⚠ 준비시간 1시간 안에 실행 — 채점 중엔 못 돌림)

```bash
cd tools
python turn.py <CloudFront endpoint>
# 물어보는 것: "카펜터 추가 노드 상한 (기본 4)" → 엔터(= 비용 천장 = stress 버스트 몇 노드까지 허용)
# → 측정(CloudFront=채점 실제 경로) → 리소스/HPA/Karpenter 계산 → 적용
# → 단일 가벼운 검증(참고용, 값 안 바꿈) → 노드 2대로 수렴 → 프리즈
# → 채점 2시간은 이 정책으로 HPA/Karpenter가 자율 대응
```

> **핵심**: turn.py는 **준비시간에만** 돌린다(채점 중 재튜닝 불가). **단일파드 깨끗한 실측**으로 파드 특성(cpu/rps/지연)을 재고 → HPA·리소스·Karpenter 정책을 산출·적용 → 프리즈. 재검증/에스컬레이션 없음(부하로 값을 안 흔듦). 노드 타입 자동 감지. **AWS CLI 자격증명 필요**(kubectl의 EKS 인증에 쓰임).

> **측정 경로**: **CloudFront(채점기가 실제로 때리는 경로)**로 측정 → user/stress 지연이 CDN 포함 실제값. product는 캐시라 이 경로에선 파드 부하가 안 잡히지만, 채점에서도 캐시라 부하가 낮음 → product는 **io 고정정책(request 100m + util 80)**으로 안정화(폭증 방지). 별도 ALB 조회 불필요.
>
> **stress**: 요청 1개가 코어를 다 씀(CPU-hog) → util 목표를 `실사용/request`로 측정기반 산정 → HPA가 **동시요청 수만큼만** 파드 확보(약한 부하 폭증 X, 버스트만 스케일). 성능(1s)은 하드웨어 한계(2코어), 가용성(5s)은 이 정책으로 최대화.

### ⑤ 채점 2시간 — 자율 운영

```bash
# 기본: ④에서 보정된 HPA(CPU)+Karpenter+warm baseline이 부하 따라 자율 대응.
#   → 정상/완만한 부하는 이걸로 충분 (성능·비용 자동균형).

# (권장) 랜덤 급증 스파이크 방어 — scaler.py 프론트러너
python scaler.py <CloudFront endpoint>
#   · 능동 프로브로 레이턴시 직접 측정(앱 바이너리 무관, 로그 장님 없음)
#   · 심각 스파이크 감지 → Karpenter 노드부팅을 HPA(15s)보다 ~13초 먼저 유발 → 큐잉 단축
#   · 가벼운 부하는 MNG 이내(노드 0), 스파이크 끝나면 감쇠 → 노드 회수(비용 회복)
#   · ⚠ 반드시 endpoint 인자 줄 것(없으면 로그기반=앱 바뀌면 장님)

# (선택) 모니터링
python dashboard.py    # 웹 대시보드
python podlog.py       # 터미널 UI (로그/파드/WAF)
```

---

## 툴 정리

| 툴 | 용도 | 언제 |
|---|---|---|
| `turn.py <endpoint>` | 측정+부하램프 자동보정(HPA/Karpenter를 SLO 최소비용에 수렴) | **준비시간 1회** (채점 중 불가) |
| `scaler.py <endpoint>` | (권장) 랜덤 스파이크 프론트러너 — 능동 프로브+노드 조기유발 | 채점 2시간, 급증 대비 |
| `dashboard.py` / `podlog.py` | 모니터링 | 선택 |
| `update_waf.py` | 헤더 이름 화이트리스트(잔여원) | **기본 미사용**. 잔여 감수 시만. 해제 `--remove` |

**스케일 2층 구조**:
1. **기반(자립)** — 준비시간에 turn.py가 **HPA(CPU)+Karpenter+warm baseline**을 부하 램프로 "SLO 지키는 최소비용" 정책에 수렴시켜 프리즈. 채점 중 정상/완만 부하는 이걸로 자율 대응.
2. **스파이크 방어(권장)** — `scaler.py <endpoint>`가 능동 프로브로 레이턴시를 직접 재다가 **심각 급증** 시 Karpenter 노드를 HPA보다 ~13초 먼저 유발(가벼운 부하엔 노드 0, 끝나면 감쇠). 기반이 못 잡는 랜덤 급증만 보완.

---

## 채점 기준 (40점)

| 항목 | 배점 | 대응 |
|---|---|---|
| 비정상 요청 처리 | 4 | WAF (비정상 403 / 없는 경로 404) |
| 가용성 | 12 | MNG 2대 분산 + readiness gate + request 격리 |
| 성능 | 12 | product 캐싱 + user warm 상주 + stress 2코어 버스트 + 부하램프 보정 |
| 비용 | 12 | MNG-fit 상주(예산 내 노드 0) + Karpenter 하드캡 + churn 완화 |

> 가용성(12) ≫ 비정상(4) → **정상 트래픽 절대 안 막힘** 최우선.

---

## 문제 생기면

```bash
# WAF가 정상 트래픽 막는 것 같으면 — 어떤 룰에 걸렸는지 확인
#   AWS 콘솔 → WAF → apdev-cf-acl → Sampled requests

# scaler 로그가 안 뜨면 (파드 로그 포맷 문제)
kubectl logs -n apdev <pod> --tail=5    # status/dur_ms 있는지 확인

# 노드가 안 줄어들 때 → Karpenter가 20초 뒤 자동 정리 (consolidation)
kubectl get nodes

# HPA 상태
kubectl get hpa -n apdev
```

---

## 참고

- **엔드포인트는 http가 https보다 빠름** — 튜닝/채점 입력 시 참고
- 노드 타입 바뀌어도(m5/xlarge 등) turn.py가 CPU/메모리 자동 감지해 대응
- WAF(base, waf.tf) = **잔여 0 화이트리스트**: 정상이 정의상 항상 만족하는 **메서드+경로**만 허용(default block) → 정상 100% 통과(증명 가능). 공격은 query/body/**모든 헤더 값**의 패턴 블랙리스트로 403, 없는경로 404. CT/바디내용/헤더이름은 '미래 정상 변형'을 막는 잔여원이라 **안 봄**.
- `update_waf.py`(헤더 이름 화이트리스트, 커버리지 게이팅판)는 **잔여원이라 기본 미사용**. 헤더 이름까지 조이고 싶고 소량 잔여를 감수할 때만 실행(이상 시 `--remove`). 만점 안전 우선이면 **켜지 말 것**.
