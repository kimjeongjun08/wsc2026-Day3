# 만점 구성 (x0.5 기준, 실측 40.0/40)

2026-08-19 실측. 채점 서버 `labuser104`, 15분 정식 회차.

```
비정상 처리    4.0 / 4
고가용성      12.0 / 12
성능 효율성   12.0 / 12     user 93.27%  product 111.31%  stress 92.39%
비용 최적화   12.0 / 12     avg_ec2 = 2.00  (cost_ratio 1.000)
────────────────────────
합계          40.0 / 40  →  100.0 / 100
```

## 구성

| 항목 | 값 | 이유 |
|---|---|---|
| EC2 인스턴스 | **2대** (t3.medium) | 비용 지표는 계정 running EC2 전체 수. 2대 = cost_ratio 1.0 = 만점 |
| bastion | **없음** | t3.small 은 "t3.medium 타입만" 위반 + 미사용 EC2 감점 + 비용 1대 |
| stress 배치 | **동거** (전용 노드 없음) | 전용 노드 1대는 비용 2점. x0.5 의 stress 수요(1.25rps)는 노드 하나를 쓸 만큼이 아니다 |
| stress cpu requests | **100m** (기본 600m) | CFS 는 requests 비율로 CPU 를 나눈다. 낮출수록 user 몫이 는다 |
| stress cpu limits | 2 (기본값 유지) | requests 는 상한이 아니다. 상한은 건드리지 않는다 |
| minDomains | 2 | 도메인이 1개면 skew 가 항상 0 이라 파드가 Pending 안 되고 Karpenter 가 안 깨어난다 |
| NodePool limits.cpu | 2 (Karpenter 1대분) | 상한 없으면 HPA 가 늘리는 만큼 노드가 붙는다 (실측 9대) |
| HPA | 기본값 유지 | maxReplicas 20→6 으로 바꿔도 지연·처리량 변화 없었다 |
| 인스턴스 타입/DB/앱 | **변경 없음** | 과제 제약 |

## 적용 순서

```bash
terraform apply -auto-approve                      # 설치 (bastion 이 DB 시딩·Karpenter 설치)
terraform apply -var bastion_enabled=false         # 설치 끝나면 bastion 제거
aws eks update-kubeconfig --region ap-northeast-2 --name apdev-cluster
./apply.sh 2 shared                                # 노드 2대, stress 동거
./tune_requests.sh 100m                            # stress CPU 지분 낮추기
```

## stress cpu requests 의 효과 (실측, 같은 2노드 동거 구성)

| requests | user | stress | 성능 | 합계 |
|---|---|---|---|---|
| 600m (기본) | 84.73% | 94.64% | 10.5 | 38.5 |
| 200m | 89.29% | 93.78% | 11.5 | 39.5 |
| **100m** | **93.27%** | **92.39%** | **12.0** | **40.0** |

user 는 +8.5%p 오르고 stress 는 −2.3%p 만 내려간다. stress SLA 는 1000ms 로 느슨하고
실측 p50 이 575ms 라 여유가 있기 때문이다. stress 가 90% 티어를 깨면 이득이 사라지므로
반드시 두 값을 같이 보고 조절해야 한다.

## 주의

이 구성은 **x0.5 트래픽(user 22, product 24, stress 1.25 rps)에 대한 최적값**이다.
트래픽이 달라지면 답이 달라진다. 특히:

- 트래픽이 커지면 코어가 모자라 노드를 늘려야 한다
- stress 요청률이 오르면(파드 1개가 4~5rps 에서 포화) 전용 노드가 다시 이득이 된다

`solve.py --traffic '...'` 으로 그때그때 다시 계산할 것.
