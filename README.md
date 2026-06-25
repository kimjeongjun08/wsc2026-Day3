# ⚠️ 시작 전 필수: `terraform/application/{user,product,stress}/` 바이너리 + `terraform/load_user.dump` 파일을 실제 대회용으로 교체하세요!

---

# WSC2026 Day3 - 클라우드컴퓨팅 3과제

## 아키텍처 개요

```
CloudFront (WAF) → ALB → EKS (HPA + Karpenter) → RDS Proxy → RDS MySQL
                                ↑
                         S3 (images)
```

- **CloudFront**: CDN + WAF (화이트리스트 default block)
- **ALB**: user/product/stress 라우팅
- **EKS**: MNG 1대 고정 + Karpenter 오토스케일
- **RDS**: MySQL 8.0, Multi-AZ, RDS Proxy 연결 풀링
- **S3**: images 버킷 (앱 동작), setup 버킷 (배포용 임시), alb-logs 버킷 (ALB 로그)

---

## 대회 당일 교체 파일

| 파일 | 위치 |
|---|---|
| user 바이너리 | `terraform/application/user/user` |
| product 바이너리 | `terraform/application/product/product` |
| stress 바이너리 | `terraform/application/stress/stress` |
| DB dump | `terraform/load_user.dump` |

---

## 준비 1시간: 인프라 구성

### 1. Terraform apply

```bash
cd terraform

# terraform.tfvars 또는 -var 로 필수값 지정
terraform apply \
  -var="node_instance_type=t3.medium" \
  -var="db_instance_class=db.t3.micro" \
  -var="db_allocated_storage=200"
```

완료 후 출력값 확인:
```bash
terraform output
# endpoint          → CloudFront 엔드포인트 (k6, autotune에 사용)
# alb_dns           → ALB DNS
# alb_logs_bucket   → ALB 로그 버킷명 (dashboard에서 자동 감지)
```

### 2. setup.sh 자동 실행 확인

bastion EC2에 접속해서 진행 상황 확인:
```bash
# AWS 콘솔 → EC2 → Session Manager 접속
tail -f /home/ec2-user/setup.log
```

`=== SETUP COMPLETE ===` 출력되면 완료.

완료되는 것들:
- MySQL 테이블 생성 + dump 로드
- ECR 이미지 빌드/푸시 (user, product, stress)
- EKS: LBC 설치, Karpenter 설치, 앱 배포, HPA 적용

### 3. Autotune (HPA/Karpenter 튜닝)

```bash
cd tools
python autotune.py <CloudFront endpoint>

# 입력:
# 노드 타입: t3.medium (대회 환경에 맞게)
# 최대 노드 수: 4 (MNG 1 + Karpenter 최대 3)
```

내부 동작:
1. 20초 워밍업 부하 → 실측 CPU 측정
2. HPA maxReplicas/request/util 계산
3. Karpenter NodePool limits 계산
4. 확인 후 y → 적용 + 45초 검증
5. 카펜터 노드 cordon → rollout restart → drain → MNG 1대로 수렴

---

## 채점 2시간: 트래픽 수신

### 실행 순서

**터미널 1: WAF 헤더 화이트리스트 (트래픽 시작 직후)**
```bash
cd tools
python update_waf.py
# 90초 수집 후 허용 헤더 화이트리스트 적용
# → 비정상 헤더 요청 차단
```

**터미널 2: 응답시간/5xx 기반 보조 스케일러 (2시간 내내)**
```bash
cd tools
python scaler.py
# 파드 로그 스트리밍 → 문제 파드 재시작 + minReplicas 보조
```

**터미널 3: HPA util 자동 최적화 (수렴하면 자동 종료)**
```bash
cd tools
python adaptive.py
# 60초마다 p95 측정 → SLO 기반 HPA util 자동 조정
# SLO 달성 + 안정 3회 연속 → 수렴 완료 후 자동 종료
```

**터미널 4: 모니터링 대시보드**
```bash
cd tools
python dashboard.py
# http://localhost:9090
```

**터미널 5: 파드 로그 모니터링 (선택)**
```bash
cd tools
python podlog.py
```

---

## 채점 기준 (총 40점)

| 항목 | 배점 | 기준 |
|---|---|---|
| 1. 비정상 요청 처리 | 4점 | Image/Exception 처리율 50~90% |
| 2. 고가용성/안정성 | 12점 | user/product/stress availability 30~90% |
| 3. 성능 효율성 | 12점 | user/product ≤0.2s, stress ≤1.0s 처리율 30~90% |
| 4. 비용 최적화 | 12점 | cost ratio 0.5~3.75 (모든 API performance 30% 이상 조건) |

**전략**: 성능(3번) 배점이 비용(4번)의 2배 → 성능 우선, 트래픽 없을 때 노드 최소화

---

## 툴 상세

### autotune.py
- **위치**: `tools/autotune.py`
- **용도**: HPA/Karpenter 최적값 자동 계산 및 적용
- **실행**: `python autotune.py <endpoint>`

### scaler.py
- **위치**: `tools/scaler.py`
- **용도**: 파드별 응답시간/5xx 기반 문제 파드 재시작 + minReplicas 보조
- **실행**: `python scaler.py`

### adaptive.py
- **위치**: `tools/adaptive.py`
- **용도**: 실제 채점 트래픽 기반 HPA util 자동 최적화 (SLO 수렴)
- **실행**: `python adaptive.py`
- **동작**: 60초마다 p95 측정 → SLO 미달 시 util↓, 여유 시 util↑ → 안정 3회 연속 시 수렴 종료

### update_waf.py
- **위치**: `tools/update_waf.py`
- **용도**: WAF 헤더 화이트리스트 룰 적용 (채점 트래픽 시작 직후 실행)
- **실행**: `python update_waf.py`
- **소요시간**: 90초 (헤더 수집) + WAF 전파 ~30초

### dashboard.py
- **위치**: `tools/dashboard.py`
- **용도**: ALB/RDS/WAF/HPA/Karpenter 종합 모니터링
- **실행**: `python dashboard.py` → http://localhost:9090

### podlog.py
- **위치**: `tools/podlog.py`
- **용도**: 파드별 실시간 로그 + SLO/SLI 대시보드 (터미널 UI)
- **실행**: `python podlog.py`

---

## k6 부하 테스트 (연습용)

```bash
cd test/load
k6 run -e BASE_URL=<endpoint> k6.js

# 30분 시나리오:
# 1회차 (0~15분): 완만한 패턴 (최대 150 VU)
# 2회차 (15~30분): 공격적 패턴 (최대 250 VU, 스파이크 포함)
# + 비정상 트래픽 28분간 상시 (WAF 우회 시도)
```

---

## 주요 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `node_instance_type` | - | EKS 노드 타입 (필수) |
| `db_instance_class` | - | RDS 인스턴스 타입 (필수) |
| `db_allocated_storage` | - | RDS 스토리지 GB (필수) |
| `node_desired_size` | 1 | MNG 초기 노드 수 |
| `node_max_size` | 4 | MNG 최대 노드 수 |

---

## 트러블슈팅

**카펜터 노드가 안 사라질 때**
```bash
# 카펜터 노드에 남은 파드 확인
kubectl get pods -A --field-selector spec.nodeName=<카펜터노드명>

# cordon → drain
kubectl cordon <노드명>
kubectl drain <노드명> --ignore-daemonsets --delete-emptydir-data --grace-period=30
```

**HPA가 스케일 안 될 때**
```bash
kubectl describe hpa -n apdev
# CPU util 확인, request 대비 실제 사용량 체크
# autotune 재실행 권장
```

**WAF 403이 계속 날 때**
```bash
# BlockUnknownHeaders 룰 제거
python -c "
import boto3, json
waf = boto3.client('wafv2', region_name='us-east-1')
resp = waf.get_web_acl(Name='apdev-cf-acl', Scope='CLOUDFRONT', Id='<ID>')
rules = [r for r in resp['WebACL']['Rules'] if r['Name'] != 'BlockUnknownHeaders']
waf.update_web_acl(Name='apdev-cf-acl', Scope='CLOUDFRONT', Id='<ID>',
    LockToken=resp['LockToken'], DefaultAction=resp['WebACL']['DefaultAction'],
    VisibilityConfig=resp['WebACL']['VisibilityConfig'], Rules=rules)
"
```
