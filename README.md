# ⚠️ 시작 전 필수: `terraform/application/{user,product,stress}/` 바이너리 + `terraform/application/load_user.dump` 파일을 실제 대회용으로 교체하세요!

---

# WSC2026 Day3 - 클라우드컴퓨팅 3과제

## 아키텍처 개요

```
CloudFront (WAF) → ALB → EKS (HPA + Karpenter) → RDS Proxy → RDS MySQL
                                ↑
                         S3 (images)
```

- **CloudFront**: CDN + WAF (화이트리스트 default block)
- **ALB**: user/product/stress 라우팅 (deregistration delay 30s)
- **EKS**: MNG 1대 고정 + Karpenter 오토스케일 (budget 100%)
- **RDS**: MySQL 8.0, Multi-AZ, RDS Proxy 연결 풀링 (90%/30%)
- **S3**: images (앱 동작), setup (배포 임시, 완료 후 삭제), alb-logs (ALB 로그)

---

## 대회 당일 교체 파일

| 파일 | 위치 |
|---|---|
| user 바이너리 | `terraform/application/user/user` |
| product 바이너리 | `terraform/application/product/product` |
| stress 바이너리 | `terraform/application/stress/stress` |
| DB dump | `terraform/application/load_user.dump` |

---

## 준비 1시간: 인프라 구성

### 1. Terraform apply

```bash
cd terraform
terraform apply \
  -var="node_instance_type=t3.medium" \
  -var="db_instance_class=db.t3.micro" \
  -var="db_allocated_storage=200"
```

완료 후:
```bash
terraform output
# endpoint          → CloudFront 엔드포인트
# alb_dns           → ALB DNS
# alb_logs_bucket   → ALB 로그 버킷명
```

### 2. setup.sh 완료 확인

```bash
# bastion EC2 접속 (Session Manager)
tail -f /home/ec2-user/setup.log
# === SETUP COMPLETE === 출력까지 대기
```

### 3. Preflight 검증

```bash
cd tools
python preflight.py <CloudFront endpoint>
# 인프라 + API + WAF 전체 헬스체크
# ✅ 전체 통과 확인 후 다음 단계
```

### 4. Autotune (HPA/Karpenter 튜닝)

```bash
python autotune.py <CloudFront endpoint>
# 노드 타입, 최대 노드 수 입력
# → 워밍업 → 계산 → 적용 → 검증 → MNG 1대로 수렴
```

---

## 채점 2시간: 트래픽 수신

### 실행 순서 (터미널 3~4개)

**터미널 1: WAF 헤더 화이트리스트 (트래픽 시작 직후 1회)**
```bash
python update_waf.py
# 90초 헤더 수집 → 화이트리스트 적용 → 완료 후 종료
```

**터미널 2: scaler (2시간 내내 실행)**
```bash
python scaler.py
```
역할:
- 1초마다: 파드별 응답시간/5xx 감지 → 나쁜 파드 교체
- 30초마다: 카펜터 노드 파드 1~2개 남으면 MNG로 이동 → 노드 정리
- 60초마다: p95 기반 HPA util 자동 조정 (adaptive, SLO 수렴)
- Pending 파드 감지 → 오래된 파드 교체로 자리 확보

**터미널 3: 모니터링 (선택)**
```bash
python dashboard.py    # http://localhost:9090 (웹 대시보드)
# 또는
python podlog.py       # 터미널 UI (로그 + WAF + 파드 상태)
```

---

## 채점 기준 (총 40점)

| 항목 | 배점 | 기준 |
|---|---|---|
| 1. 비정상 요청 처리 | 4점 | Image/Exception 처리율 50~90% |
| 2. 고가용성/안정성 | 12점 | user/product/stress availability 30~90% |
| 3. 성능 효율성 | 12점 | user/product ≤0.2s, stress ≤1.0s 처리율 30~90% |
| 4. 비용 최적화 | 12점 | cost ratio 0.5~3.75 (모든 API performance 30% 이상 조건) |

**전략**: 성능(24점) > 비용(12점) → 성능 우선, 트래픽 없을 때 노드 최소화

---

## 툴 목록

| 툴 | 용도 | 실행 시점 |
|---|---|---|
| `preflight.py` | 인프라+API+WAF 전체 헬스체크 | setup 완료 후, autotune 전 |
| `autotune.py` | HPA/Karpenter 최적값 계산+적용 | 준비 시간 (1회) |
| `prewarm.py` | 트래픽 직전 사전 스케일(콜드스타트 방지), `--reset` 복귀 | 트래픽 시작 3~5분 전 |
| `update_waf.py` | WAF 헤더 화이트리스트 적용 | 채점 트래픽 시작 직후 (1회) |
| `scaler.py` | HPA floor(minReplicas) 보조 + adaptive util (노드정리는 Karpenter에 일임) | 채점 2시간 내내 |
| `wafcheck.py` | WAF 로그 분석 → 정상 트래픽 오차단(자기-403) 감지 | 트래픽 중 수시 |
| `costcheck.py` | 실행 인스턴스 실측 → cost ratio 추정/노드 절감 판단 | 트래픽 중 수시 |
| `dashboard.py` | 웹 대시보드 (ALB/RDS/WAF/HPA) | 선택 |
| `podlog.py` | 터미널 UI (로그+WAF+파드 상태) | 선택 |

---

## scaler.py 상세 기준

**파드 교체 (30초 윈도우):**
| 앱 | 트리거 |
|---|---|
| user/product | 평균 ≥ 0.4초 OR 5xx ≥ 10개 |
| stress | 평균 ≥ 2.0초 OR 5xx ≥ 10개 |

**adaptive HPA util 조정 (60초 주기):**
- p95 > SLO (user/product 0.2초, stress 1.0초) → util -5%
- p95 < SLO × 80% → util +5%
- 안정 3회 연속 → 수렴 완료 (트래픽 패턴 바뀌면 재조정)
- util 범위: user/product 40~85%, stress 30~70%

---

## k6 부하 테스트 (연습용)

```bash
cd test/load
k6 run -e BASE_URL=<endpoint> k6.js
```

---

## 주요 변수 (terraform.tfvars)

| 변수 | 설명 |
|---|---|
| `node_instance_type` | EKS 노드 타입 (필수) |
| `db_instance_class` | RDS 인스턴스 타입 (필수) |
| `db_allocated_storage` | RDS 스토리지 GB (필수) |
| `node_max_size` | MNG 최대 노드 수 (기본 4) |

---

## 트러블슈팅

**카펜터 노드가 안 사라질 때**
```bash
kubectl get pods -A --field-selector spec.nodeName=<노드명>
# 파드 있으면: scaler.py가 30초 내 정리
# 안되면 수동: kubectl cordon + kubectl drain --ignore-daemonsets --force
```

**HPA 스케일 안 될 때**
```bash
kubectl describe hpa -n apdev
# autotune 재실행 권장
```

**WAF 403 지속**
```bash
# BlockUnknownHeaders 룰 제거 후 update_waf.py 재실행
```

**5xx 발생 시**
```bash
# scaler.py가 자동으로 문제 파드 교체
# 대시보드에서 어떤 파드인지 확인: dashboard.py → Errors 탭
```
