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

# product GET 캐시 정책: 캐시 키는 id 쿼리스트링만 (requestid/uuid 는 매 요청 바뀌므로 제외).
# min/default_ttl=0, max_ttl=10 → 오리진이 Cache-Control 을 준 응답만 (product 앱은 DB 조회=캐시미스
# 경로에서만 "public, max-age=10" 을 붙임) 최대 10초 엣지 캐싱. Cache-Control 없는 응답(404 등)은
# default_ttl=0 이라 캐싱되지 않음 → 존재하지 않는 id 의 404 가 캐시에 눌러앉는 사고 방지.
# 동일 id 반복 GET(과제지 명시 패턴)이 오리진/DB 를 안 타고 엣지에서 처리 → 성능 SLO 여유 + 노드 절감.
resource "aws_cloudfront_cache_policy" "product_get" {
  name        = "${local.name}-product-get"
  min_ttl     = 0
  default_ttl = 0
  max_ttl     = 10

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true
    cookies_config { cookie_behavior = "none" }
    headers_config { header_behavior = "none" }
    query_strings_config {
      query_string_behavior = "whitelist"
      query_strings { items = ["id"] }
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

  # /v1/product → ALB. GET 만 엣지 캐싱(id 기준, 최대 10초). POST/PUT 는 CloudFront 가
  # 캐싱하지 않고 매번 오리진으로 전달되므로 생성/이미지변경은 즉시 반영된다.
  ordered_cache_behavior {
    path_pattern             = "/v1/product"
    target_origin_id         = "alb"
    viewer_protocol_policy   = "allow-all"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    compress                 = true
    cache_policy_id          = aws_cloudfront_cache_policy.product_get.id
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
