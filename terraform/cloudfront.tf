resource "aws_cloudfront_origin_access_control" "s3" {
  name                              = "apdev-s3-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "main" {
  enabled         = true
  comment         = "apdev-cdn"
  price_class     = "PriceClass_200"
  is_ipv6_enabled = true

  origin {
    domain_name              = aws_s3_bucket.images.bucket_regional_domain_name
    origin_id                = "s3-images"
    origin_access_control_id = aws_cloudfront_origin_access_control.s3.id
    origin_path              = ""
  }

  default_cache_behavior {
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    target_origin_id       = "s3-images"
    viewer_protocol_policy = "allow-all"
    compress               = true

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    min_ttl     = 0
    default_ttl = 86400
    max_ttl     = 31536000
  }

  ordered_cache_behavior {
    path_pattern           = "/images/*"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    target_origin_id       = "s3-images"
    viewer_protocol_policy = "allow-all"
    compress               = true

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    min_ttl     = 0
    default_ttl = 86400
    max_ttl     = 31536000
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = { Name = "apdev-cdn" }
}

# S3 bucket policy for CloudFront OAC
resource "aws_s3_bucket_policy" "images_cf" {
  bucket = aws_s3_bucket.images.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudFrontOAC"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.images.arn}/*"
        Condition = {
          StringEquals = {
            "AWS:SourceArn" = aws_cloudfront_distribution.main.arn
          }
        }
      }
    ]
  })
}

# WAF WebACL (REGIONAL, ap-northeast-2)
resource "aws_wafv2_web_acl" "main" {
  name  = "apdev-waf"
  scope = "REGIONAL"

  default_action {
    block {}
  }

  # Rule 1: Allow valid GET
  rule {
    name     = "AllowValidGET"
    priority = 1
    action {
      allow {}
    }
    statement {
      and_statement {
        statement {
          byte_match_statement {
            search_string         = "GET"
            field_to_match { method {} }
            positional_constraint = "EXACTLY"
            text_transformation { priority = 0; type = "NONE" }
          }
        }
        statement {
          or_statement {
            statement {
              byte_match_statement {
                search_string         = "/v1/user"
                field_to_match { uri_path {} }
                positional_constraint = "EXACTLY"
                text_transformation { priority = 0; type = "NONE" }
              }
            }
            statement {
              byte_match_statement {
                search_string         = "/v1/product"
                field_to_match { uri_path {} }
                positional_constraint = "EXACTLY"
                text_transformation { priority = 0; type = "NONE" }
              }
            }
            statement {
              byte_match_statement {
                search_string         = "/healthcheck"
                field_to_match { uri_path {} }
                positional_constraint = "EXACTLY"
                text_transformation { priority = 0; type = "NONE" }
              }
            }
            statement {
              byte_match_statement {
                search_string         = "/images/"
                field_to_match { uri_path {} }
                positional_constraint = "STARTS_WITH"
                text_transformation { priority = 0; type = "NONE" }
              }
            }
          }
        }
      }
    }
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "AllowValidGET"
    }
  }

  # Rule 2: Allow valid POST/PUT
  rule {
    name     = "AllowValidPOST"
    priority = 2
    action {
      allow {}
    }
    statement {
      and_statement {
        statement {
          or_statement {
            statement {
              byte_match_statement {
                search_string         = "POST"
                field_to_match { method {} }
                positional_constraint = "EXACTLY"
                text_transformation { priority = 0; type = "NONE" }
              }
            }
            statement {
              byte_match_statement {
                search_string         = "PUT"
                field_to_match { method {} }
                positional_constraint = "EXACTLY"
                text_transformation { priority = 0; type = "NONE" }
              }
            }
          }
        }
        statement {
          or_statement {
            statement {
              byte_match_statement {
                search_string         = "/v1/user"
                field_to_match { uri_path {} }
                positional_constraint = "EXACTLY"
                text_transformation { priority = 0; type = "NONE" }
              }
            }
            statement {
              byte_match_statement {
                search_string         = "/v1/product"
                field_to_match { uri_path {} }
                positional_constraint = "EXACTLY"
                text_transformation { priority = 0; type = "NONE" }
              }
            }
            statement {
              byte_match_statement {
                search_string         = "/v1/stress"
                field_to_match { uri_path {} }
                positional_constraint = "EXACTLY"
                text_transformation { priority = 0; type = "NONE" }
              }
            }
          }
        }
      }
    }
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "AllowValidPOST"
    }
  }

  # Rule 3: Block SQLi
  rule {
    name     = "BlockSQLi"
    priority = 10
    action {
      block {}
    }
    statement {
      or_statement {
        statement {
          sqli_match_statement {
            field_to_match { query_string {} }
            text_transformation { priority = 0; type = "URL_DECODE" }
            text_transformation { priority = 1; type = "LOWERCASE" }
          }
        }
        statement {
          sqli_match_statement {
            field_to_match { body { oversize_handling = "CONTINUE" } }
            text_transformation { priority = 0; type = "URL_DECODE" }
            text_transformation { priority = 1; type = "LOWERCASE" }
          }
        }
      }
    }
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "BlockSQLi"
    }
  }

  # Rule 4: Block XSS
  rule {
    name     = "BlockXSS"
    priority = 11
    action {
      block {}
    }
    statement {
      or_statement {
        statement {
          xss_match_statement {
            field_to_match { query_string {} }
            text_transformation { priority = 0; type = "URL_DECODE" }
            text_transformation { priority = 1; type = "HTML_ENTITY_DECODE" }
          }
        }
        statement {
          xss_match_statement {
            field_to_match { body { oversize_handling = "CONTINUE" } }
            text_transformation { priority = 0; type = "URL_DECODE" }
            text_transformation { priority = 1; type = "HTML_ENTITY_DECODE" }
          }
        }
      }
    }
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "BlockXSS"
    }
  }

  # Rule 5: Block bad UA / scanners
  rule {
    name     = "BlockBadUA"
    priority = 12
    action {
      block {}
    }
    statement {
      regex_match_statement {
        regex_string = "(nikto|sqlmap|nmap|masscan|dirbuster|gobuster|wfuzz|hydra|burp|nessus|acunetix|zgrab|nuclei|feroxbuster|striker|python-requests)"
        field_to_match {
          single_header { name = "user-agent" }
        }
        text_transformation { priority = 0; type = "LOWERCASE" }
      }
    }
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "BlockBadUA"
    }
  }

  visibility_config {
    sampled_requests_enabled   = true
    cloudwatch_metrics_enabled = true
    metric_name                = "apdev-waf"
  }

  tags = { Name = "apdev-waf" }
}
