# 1시간 런북 — 시계 보면서 따라가는 문서

대회는 **인프라 구축 1시간**을 주고, 정각에 트래픽이 들어온다.
그 순간 시스템이 안정 상태여야 한다 — 배포가 돌고 있으면 첫 분부터 5xx 가 나고 가용성 12점이 깎인다.

이 문서는 **시간표만** 다룬다. 도구 사용법과 원리는 `README.md` 를 봐라.

모든 소요시간은 2026-08-19 실측이다.

---

## 전체 시간표

| 시각 | 할 일 | 소요 | 사람이 붙어 있어야 하나 |
|---|---|---|---|
| 0분 | `terraform apply` | 18분 | 시작만 하고 대기 |
| 20분 | bastion 제거 + kubectl 연결 | 5분 | 붙어 있어야 함 |
| 25분 | `./GO.sh` | 30분 | 시작만 하고 대기 |
| 55분 | 엔드포인트 등록 + 확인 | 5분 | **반드시 붙어 있어야 함** |
| 60분 | `./GO.sh watch` | — | 켜두고 끝 |

여유는 약 5분이다. **아래 "시간이 밀리면" 을 미리 읽어둬라.**

---

## 0분 — 배포 시작

```bash
cd terraform && terraform apply -auto-approve
```

RDS Multi-AZ 가 16분으로 병목이다. 줄일 방법이 없으니 **무조건 제일 먼저** 친다.

기다리는 18분 동안 할 것:

- 채점 플랫폼 로그인해두기
- 다음 단계 명령어를 미리 복사해두기
- `export AWS_PROFILE=<본인>` 설정 확인

---

## 20분 — bastion 제거

```bash
aws eks update-kubeconfig --region ap-northeast-2 --name apdev-cluster
kubectl -n apdev get pods
```

`user` `product` `stress` 가 **Running** 이어야 다음으로 간다.

- `ImagePullBackOff` → ECR 이미지 경로 확인
- `CrashLoopBackOff` → DB 접속 환경변수 확인 (루트 `README.md` 0번 항목)
- 아무것도 없음 → bastion 의 설치 스크립트가 아직 도는 중. 2~3분 더 기다린다

앱이 떠 있으면 bastion 을 지운다.

```bash
terraform apply -var bastion_enabled=false      # 실측 3분
```

> bastion 한 대가 **비용 2점**이다. 채점은 EKS 노드가 아니라 계정의 EC2 전체 수를 센다.
> t3.small 이라 "EC2 는 t3.medium 만" 규정도 어긴다. 지우는 게 두 배로 이득이다.

---

## 25분 — 튜너 실행

```bash
cd tools/tuner
export AWS_PROFILE=<본인>
./GO.sh
```

30분간 알아서 돈다. 4단계로 진행되며 각 단계가 화면에 찍힌다.

```
1/4  준비        앱 처리 한계 측정 (앱당 1분)
2/4  탐색        후보 2개에 직접 부하를 넣어 비교 (후보당 5~8분)
3/4  적용        점수가 높은 구성으로 전환
4/4  확인        안정화 6종 통과할 때까지 대기
```

마지막에 **"준비 끝"** 이 나오면 성공이다.

### 이 30분 동안 할 것

- 채점 플랫폼 Settings 페이지 열어두기
- CloudFront 주소 확인: `terraform -chdir=../../terraform output endpoint`
- 모니터링 대시보드 확인 (과제 요구사항이면)

---

## 55분 — 엔드포인트 등록 ★가장 중요

채점 플랫폼 Settings 에 CloudFront 주소를 넣는다. **안 넣으면 0점이다.**

형식을 틀리지 마라:

```
정상    https://d1xxxx.cloudfront.net
오류    d1xxxx.cloudfront.net            (프로토콜 없음)
오류    https://d1xxxx.cloudfront.net/v1/ (경로 붙임)
```

넣고 나서 확인:

```bash
curl -o /dev/null -w '%{http_code}\n' -X POST <주소>/v1/user \
  -H 'Content-Type: application/json' -H 'User-Agent: Mozilla/5.0' \
  -d '{"requestid":"r","uuid":"u","username":"probe1","email":"probe1@x.com"}'
```

`201` 이면 정상. **`403` 이 나오면 `User-Agent` 헤더를 뺀 것이다** — WAF 가 기본 curl 을 막는다.

마지막으로 상태 확인:

```bash
./GO.sh status
```

안정화 6종이 전부 `[O]` 여야 한다.

---

## 60분 — 트래픽 시작

```bash
./GO.sh watch
```

백그라운드로 돌면서 알아서 조정한다. 여기부터는 로그만 가끔 본다.

```bash
tail -f autotune.log
```

---

# 시간이 밀리면 — 무엇부터 버리나

**순서대로 버려라. 위쪽이 더 중요하다.**

| 우선순위 | 항목 | 버렸을 때 |
|---|---|---|
| 1 (절대 사수) | 엔드포인트 등록 | **0점** |
| 2 (절대 사수) | 앱 3개 Running | 0점 |
| 3 | bastion 제거 | −2점 |
| 4 | `./GO.sh` 의 탐색 단계 | 초기 구성이 덜 정확해짐. `autotune` 이 나중에 교정 |
| 5 | 안정화 대기 | 첫 몇 분 5xx 위험 |

## 40분인데 아직 `terraform apply` 중이라면

앱이 뜨는 즉시 이 순서로 최소한만 한다.

```bash
terraform apply -var bastion_enabled=false      # 3분, 2점짜리
cd tools/tuner
./autotune.sh prepare                            # 10분, 탐색 생략
# 엔드포인트 등록
./GO.sh watch
```

`prepare` 는 곡선 측정 + 안전한 초기 구성(하한 2대 / 상한 6대)까지만 한다.
부하 비교를 건너뛰는 것뿐이고, 트래픽이 시작되면 `autotune` 이 실측으로 다시 잡는다.

## 55분인데 아무것도 못 했다면

**엔드포인트 등록만 하고 손 뗀다.** 기본 배포 상태로도 트래픽은 처리된다.
그다음 여유가 생기면 `./GO.sh watch` 만 켠다.

---

# 트래픽 시작 후 — 사람이 볼 것

기본적으로 볼 게 없다. 다만 아래 두 가지는 눈에 띄면 바로 대응한다.

## 노드가 계속 늘어난다

```bash
./GO.sh status
```

상한이 너무 높게 잡혔을 수 있다. 트래픽이 안정되면 좁힌다.

```bash
./apply.sh <하한> <배치> <하한>     # 상한을 하한까지
```

단, **트래픽이 오르내리는 중이면 좁히지 마라.** 다음 스파이크에 못 늘어난다.

## 특정 앱 성능이 30% 아래로 떨어진다

**비용 12점이 통째로 날아가는 상황이다.** 즉시 노드를 늘린다.

```bash
./apply.sh <현재+1> <배치> <현재+3>
```

`autotune watch` 가 돌고 있으면 1분 안에 알아서 한다. 안 돌고 있으면 직접.

---

# 실측 참고값

배포 시간:

| 리소스 | 실측 |
|---|---|
| RDS Multi-AZ | 16분 (병목) |
| EKS 클러스터 | 7분 |
| CloudFront | 3분 |
| 전체 `terraform apply` | 18분 |
| bastion 제거 apply | 3분 |

앱 처리 한계 (t3.medium 2코어, 파드 1개):

| 앱 | 포화 처리량 |
|---|---|
| user POST | 82 rps |
| user GET | 85 rps |
| product GET | 199 rps |
| stress POST | **4~5 rps** |

`stress` 가 20배 무겁다. 이게 노드 배치 판단의 핵심이다.
