resource "aws_cloudfront_origin_access_control" "s3" {
  name                              = "${local.name}-s3-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_function" "strip_images_prefix" {
  name    = "${local.name}-strip-images"
  runtime = "cloudfront-js-2.0"
  publish = true
  code    = <<-EOT
    function handler(event) {
      var req = event.request;
      if (req.uri.indexOf('/images/') === 0) {
        req.uri = req.uri.substring(7);
      }
      return req;
    }
  EOT
}

# S3 이미지: 최대 캐시 + 최소 레이턴시
resource "aws_cloudfront_cache_policy" "images" {
  name        = "${local.name}-images"
  min_ttl     = 2592000
  default_ttl = 2592000
  max_ttl     = 2592000

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true
    cookies_config { cookie_behavior = "none" }
    headers_config { header_behavior = "none" }
    query_strings_config { query_string_behavior = "none" }
  }
}

# product GET 캐시 정책: 캐시키 = query "id"만 (requestid/uuid 무시 → 동일 id 요청 캐시 적중)
# 문제지: "동일 id 빈번 요청 + 정보 변동 거의 없음" → 캐싱이 의도된 설계.
# TTL 길게(1시간): 10만 개 id에 요청이 분산돼도 반복 요청이 캐시에 남아있게 → 적중률↑.
#   안전: 채점은 상태코드+레이턴시만 봄 + 정보 변동 거의 없음 + 새 상품은 첫 GET에서 캐시(stale 아님)
#         + 에러 캐싱 1초(custom_error_response)라 생성 직후 404 문제없음.
resource "aws_cloudfront_cache_policy" "product" {
  name        = "${local.name}-product"
  min_ttl     = 1
  default_ttl = 3600
  max_ttl     = 3600

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true
    cookies_config { cookie_behavior = "none" }
    headers_config { header_behavior = "none" }
    query_strings_config {
      query_string_behavior = "whitelist"
      query_strings {
        items = ["id"]
      }
    }
  }
}

# Origin request policy: 모든 쿼리스트링/헤더 전달 (ALB pass-through)
resource "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "${local.name}-all-viewer"
  cookies_config { cookie_behavior = "all" }
  headers_config {
    header_behavior = "whitelist"
    headers {
      items = ["Content-Type", "Content-Length"]
    }
  }
  query_strings_config { query_string_behavior = "all" }
}

resource "aws_cloudfront_distribution" "this" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "${local.name} CDN"
  http_version    = "http2and3"
  price_class     = "PriceClass_200"
  web_acl_id      = aws_wafv2_web_acl.cloudfront.arn

  origin {
    domain_name              = aws_s3_bucket.images.bucket_regional_domain_name
    origin_id                = "s3-images"
    origin_access_control_id = aws_cloudfront_origin_access_control.s3.id
  }

  origin {
    domain_name = aws_lb.this.dns_name
    origin_id   = "alb"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      # CF↔ALB 커넥션을 60초 웜 유지 → 매 요청 TCP 핸드셰이크 제거(레이턴시 꼬리↓).
      # read 60초 → 느린 stress 요청이 30초 기본값에서 504 나던 것 방지(5xx→처리). 둘 다 리스크 0.
      origin_keepalive_timeout = 60
      origin_read_timeout      = 60
    }
  }

  # /images/* → S3 (장기 캐시)
  ordered_cache_behavior {
    path_pattern           = "/images/*"
    target_origin_id       = "s3-images"
    viewer_protocol_policy = "allow-all"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = aws_cloudfront_cache_policy.images.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.strip_images_prefix.arn
    }
  }

  # /v1/product → GET만 캐시(id별), POST/PUT는 통과(cached_methods=GET/HEAD라 자동)
  ordered_cache_behavior {
    path_pattern             = "/v1/product"
    target_origin_id         = "alb"
    viewer_protocol_policy   = "allow-all"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    compress                 = true
    cache_policy_id          = aws_cloudfront_cache_policy.product.id
    origin_request_policy_id = aws_cloudfront_origin_request_policy.all_viewer.id
  }

  # default → ALB pass-through (캐시 비활성화)
  default_cache_behavior {
    target_origin_id         = "alb"
    viewer_protocol_policy   = "allow-all"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    compress                 = true
    cache_policy_id          = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" # CachingDisabled managed policy
    origin_request_policy_id = aws_cloudfront_origin_request_policy.all_viewer.id
  }

  # 에러 응답 캐싱 최소화: 생성 직후(POST) GET이 이전 404를 물지 않게 (가용성 보호)
  custom_error_response {
    error_code            = 404
    error_caching_min_ttl = 1
  }
  custom_error_response {
    error_code            = 403
    error_caching_min_ttl = 1
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = { Name = "${local.name}-cdn" }
}
