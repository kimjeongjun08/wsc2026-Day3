# wsi-2026-task3 — Infrastructure

2026 전국기능경기대회 클라우드컴퓨팅 3과제 (System Operation) 인프라.

## 구성

| 계층 | 리소스 | 비고 |
|---|---|---|
| 네트워크 | VPC + 2-AZ public subnet (a/b) | NAT 없음, 단일 RT, IGW만 |
| 컨테이너 | EKS 1.33 + EC2 t3.medium 2~4대 node group | Fargate/Lambda 금지 준수 |
| 레지스트리 | ECR × 3 (user/product/stress) | terraform apply 시 buildx로 자동 push |
| DB | RDS MySQL 8.0 db.t3.micro Multi-AZ gp3 | identifier `apdev-rds-instance` |
| 스토리지 | S3 (private, CloudFront OAC) | 이미지 버킷 |
| 엔드포인트 | CloudFront → ALB + S3 | 단일 엔드포인트 |
| 보안 | WAFv2 (Common/KnownBadInputs/SQLi) | 비정상 요청 403 차단 |

## 효율성 설계 (채점기준 반영)

채점 12점 = **비용 ratio** + 12점 = **성능 (≤0.2s 비율)** + 12점 = **가용성** + 4점 = **비정상 요청 처리**.

### 비용 최적화 (12점)
- NAT Gateway 제거 → 월 $32+ 절감
- t3.medium 노드 2~4대 HPA (필요 시만 확장)
- 단일 NAT/Private subnet 제거로 단순화
- ECR 라이프사이클 10개

### 성능 효율성 (12점, 0.2s 이하)
- **product GET 캐싱**: 앱 `sync.Map` (10s TTL) + CloudFront 캐시 (querystring `id` 기준)
  - 같은 id 반복 요청 → DB hit 안 함 (사실상 0.001s 응답)
- **user.email 인덱스**: 스펙에 없는 인덱스를 db-init Job이 자동 추가
- **HPA**: CPU 60%/70% 기준 자동 확장
- **CloudFront `/images/*`**: S3 직접 캐싱 (앱 우회)

### 가용성 (12점)
- EKS node 2-AZ
- RDS Multi-AZ
- topology spread constraint로 pod 분산

### 비정상 요청 (4점)
- WAFv2 AWS Managed Rules → 403
- 정의 안 된 path → ALB fixed-response 404

## 배포

```bash
cd terraform
terraform init
AWS_PROFILE=lee terraform apply -auto-approve     # ~20분 (EKS + RDS 동시 생성)

terraform output endpoint
# https://dXXXXX.cloudfront.net    ← 채점 플랫폼에 입력
```

`null_resource.build_push`가 `terraform apply` 안에서 ECR 로그인 + docker buildx + push를 자동 수행. 앱 소스 변경 시 hash 변경되어 재빌드 + 재배포됨.

**Windows 환경**: `build.tf`가 bash 문법이라 WSL2 또는 Git Bash 안에서 실행해야 함.

## 데이터 로드

대회 당일 받는 `load_user.dump` 파일을 RDS에 로드:

```bash
mysql -h $(terraform output -raw rds_endpoint | cut -d: -f1) \
      -u appuser -p$(terraform output -raw db_password) dev < load_user.dump
```

## 검증된 동작

```
GET  /healthcheck                       → 200 {"ok":true}
POST /v1/user        {requestid,...}    → 201
GET  /v1/user?email=...&requestid=...   → 200 / 404
POST /v1/product     {id,name,price}    → 201
GET  /v1/product?id=...                 → 200 (2nd call cached, X-Cache: Hit)
PUT  /v1/product     multipart(id,image) → 200 (S3 upload)
GET  /images/foo.jpg                    → 200 (CloudFront → S3, URI rewrite)
POST /v1/stress      {length:N}         → 201
GET  /v1/none                           → 404
GET  /random                            → 404
```

## 정리

```bash
AWS_PROFILE=lee terraform destroy -auto-approve
```

## 파일 구조

```
terraform/
├── versions.tf / providers.tf / variables.tf / locals.tf / outputs.tf
├── vpc.tf                       # VPC + 2-AZ public subnet + IGW + S3 VPCe
├── ecr.tf                       # 3 repos + lifecycle
├── build.tf                     # null_resource: buildx + ECR push
├── rds.tf                       # MySQL 8.0 Multi-AZ
├── s3.tf                        # private bucket + CloudFront OAC
├── eks.tf                       # cluster + node group + addons
├── iam.tf + policies/           # IRSA roles
├── lb_controller.tf             # AWS LB Controller (helm)
├── k8s_base.tf                  # namespace + secret + db-init Job
├── k8s_apps.tf                  # user/product/stress Deploy+Svc+HPA
├── k8s_ingress.tf               # ALB Ingress (path routing, default 404)
├── waf.tf                       # WAFv2 web ACL
├── cloudfront.tf                # CloudFront + URI rewrite function
└── README.md
```
