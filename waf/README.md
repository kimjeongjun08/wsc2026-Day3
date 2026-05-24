# WAF 화이트리스트 룰 구성

## 전략
- **Default Action: BLOCK** (모든 요청 기본 차단)
- 화이트리스트 룰로 정상 요청만 ALLOW

## 룰 구성

### Rule 1: AllowValidGET (Priority 1)
GET 요청 화이트리스트:
- `GET /v1/user?email=...&requestid=숫자&uuid=UUID형식` (쿼리 파라미터 3개 정확히)
- `GET /v1/product?id=...&requestid=숫자&uuid=UUID형식` (쿼리 파라미터 3개 정확히)
- `GET /healthcheck`
- `GET /images/*`

검증 항목:
- requestid: 숫자만 (`^[0-9]+$`)
- uuid: UUID v4 형식
- 쿼리스트링 파라미터 개수: 정확히 3개 (`^[^&]*&[^&]*&[^&]*$`)

### Rule 2: AllowValidPOST (Priority 2)
POST/PUT 요청 화이트리스트:

**POST /v1/user:**
- body에 requestid(숫자), uuid(UUID v4), username, email(이메일형식) 포함
- body에 불필요한 필드 없음 (`:` 6개 이상이면 차단)

**POST /v1/product:**
- body에 requestid(숫자), uuid(UUID v4), id, name, price 포함
- body에 불필요한 필드 없음

**PUT /v1/product:**
- body에 requestid(숫자), uuid(UUID v4), id 포함 (multipart - 이미지 첨부)

**POST /v1/stress:**
- body에 requestid(숫자), uuid(UUID v4), length 포함
- body에 불필요한 필드 없음 (`:` 4개 이상이면 차단)

## 적용 방법

```bash
# WebACL 생성 (Default BLOCK)
aws wafv2 create-web-acl \
  --name apdev-waf \
  --scope REGIONAL \
  --default-action Block={} \
  --visibility-config SampledRequestsEnabled=true,CloudWatchMetricsEnabled=true,MetricName=apdev-waf \
  --rules file://get.json,file://post.json \
  --region ap-northeast-2

# ALB에 연결
aws wafv2 associate-web-acl \
  --web-acl-arn <WEB_ACL_ARN> \
  --resource-arn <ALB_ARN> \
  --region ap-northeast-2
```

## 비정상 요청 처리 결과
- 정의된 API 경로의 비정상 요청 → WAF BLOCK → **403**
- 정의되지 않은 경로 → WAF 통과 (GET /healthcheck, /images 외) → 앱/ingress에서 **404**

## 참고
- 과제에서 "비정상 요청은 403, 제공하는 API 외 요청은 404" 요구사항 충족
- User-Agent 검증은 트래픽 발생기에 따라 조정 필요 (k6, curl 등)
