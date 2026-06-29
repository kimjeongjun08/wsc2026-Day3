# WAF WebACL - CLOUDFRONT scope, 화이트리스트 (default BLOCK)
# 헤더 룰은 tools/update_waf.py로 추가
resource "aws_wafv2_web_acl" "cloudfront" {
  provider = aws.us_east_1
  name     = "${local.name}-cf-acl"
  scope    = "CLOUDFRONT"

  default_action {
    block {}
  }

  # Rule 1: ALLOW valid GET
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
            field_to_match {
              method {}
            }
            positional_constraint = "EXACTLY"
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          or_statement {
            # GET /v1/user?email=...&requestid=숫자&uuid=UUIDv4 (쿼리3개)
            statement {
              and_statement {
                statement {
                  byte_match_statement {
                    search_string         = "/v1/user"
                    field_to_match {
                      uri_path {}
                    }
                    positional_constraint = "EXACTLY"
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
                statement {
                  regex_match_statement {
                    regex_string = "^[a-zA-Z0-9._%+\\-]+@[a-zA-Z0-9.\\-]+\\.[a-zA-Z]{2,}$"
                    field_to_match {
                      single_query_argument {
                        name = "email"
                      }
                    }
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
                statement {
                  regex_match_statement {
                    regex_string = "^[0-9]+$"
                    field_to_match {
                      single_query_argument {
                        name = "requestid"
                      }
                    }
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
                statement {
                  regex_match_statement {
                    regex_string = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
                    field_to_match {
                      single_query_argument {
                        name = "uuid"
                      }
                    }
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
              }
            }
            # GET /v1/product?id=...&requestid=숫자&uuid=UUIDv4
            statement {
              and_statement {
                statement {
                  byte_match_statement {
                    search_string         = "/v1/product"
                    field_to_match {
                      uri_path {}
                    }
                    positional_constraint = "EXACTLY"
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
                statement {
                  regex_match_statement {
                    regex_string = "^[a-zA-Z0-9_\\-]+$"
                    field_to_match {
                      single_query_argument {
                        name = "id"
                      }
                    }
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
                statement {
                  regex_match_statement {
                    regex_string = "^[0-9]+$"
                    field_to_match {
                      single_query_argument {
                        name = "requestid"
                      }
                    }
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
                statement {
                  regex_match_statement {
                    regex_string = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
                    field_to_match {
                      single_query_argument {
                        name = "uuid"
                      }
                    }
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
              }
            }
            # GET /healthcheck
            statement {
              byte_match_statement {
                search_string         = "/healthcheck"
                field_to_match {
                  uri_path {}
                }
                positional_constraint = "EXACTLY"
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
            # GET /images/*
            statement {
              byte_match_statement {
                search_string         = "/images/"
                field_to_match {
                  uri_path {}
                }
                positional_constraint = "STARTS_WITH"
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
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

  # Rule 2: ALLOW valid POST
  rule {
    name     = "AllowValidPOST"
    priority = 2
    action {
      allow {}
    }
    statement {
      and_statement {
        statement {
          byte_match_statement {
            search_string         = "POST"
            field_to_match {
              method {}
            }
            positional_constraint = "EXACTLY"
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          byte_match_statement {
            search_string = "application/json"
            field_to_match {
              single_header { name = "content-type" }
            }
            positional_constraint = "CONTAINS"
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          or_statement {
            # POST /v1/user (requestid숫자 + uuid형식 + username + email)
            statement {
              and_statement {
                statement {
                  byte_match_statement {
                    search_string         = "/v1/user"
                    field_to_match {
                      uri_path {}
                    }
                    positional_constraint = "EXACTLY"
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
                statement {
                  regex_match_statement {
                    regex_string = "^[0-9]+$"
                    field_to_match {
                      json_body {
                        match_pattern {
                          included_paths = ["/requestid"]
                        }
                        match_scope             = "ALL"
                        invalid_fallback_behavior = "NO_MATCH"
                        oversize_handling       = "CONTINUE"
                      }
                    }
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
                statement {
                  regex_match_statement {
                    regex_string = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
                    field_to_match {
                      json_body {
                        match_pattern {
                          included_paths = ["/uuid"]
                        }
                        match_scope             = "ALL"
                        invalid_fallback_behavior = "NO_MATCH"
                        oversize_handling       = "CONTINUE"
                      }
                    }
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
                statement {
                  byte_match_statement {
                    search_string         = "username"
                    field_to_match {
                      body {
                        oversize_handling = "CONTINUE"
                      }
                    }
                    positional_constraint = "CONTAINS"
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
                statement {
                  byte_match_statement {
                    search_string         = "email"
                    field_to_match {
                      body {
                        oversize_handling = "CONTINUE"
                      }
                    }
                    positional_constraint = "CONTAINS"
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
              }
            }
            # POST /v1/product (requestid + uuid + name + price)
            statement {
              and_statement {
                statement {
                  byte_match_statement {
                    search_string         = "/v1/product"
                    field_to_match {
                      uri_path {}
                    }
                    positional_constraint = "EXACTLY"
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
                statement {
                  regex_match_statement {
                    regex_string = "^[0-9]+$"
                    field_to_match {
                      json_body {
                        match_pattern {
                          included_paths = ["/requestid"]
                        }
                        match_scope             = "ALL"
                        invalid_fallback_behavior = "NO_MATCH"
                        oversize_handling       = "CONTINUE"
                      }
                    }
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
                statement {
                  regex_match_statement {
                    regex_string = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
                    field_to_match {
                      json_body {
                        match_pattern {
                          included_paths = ["/uuid"]
                        }
                        match_scope             = "ALL"
                        invalid_fallback_behavior = "NO_MATCH"
                        oversize_handling       = "CONTINUE"
                      }
                    }
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
                statement {
                  byte_match_statement {
                    search_string         = "name"
                    field_to_match {
                      body {
                        oversize_handling = "CONTINUE"
                      }
                    }
                    positional_constraint = "CONTAINS"
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
                statement {
                  byte_match_statement {
                    search_string         = "price"
                    field_to_match {
                      body {
                        oversize_handling = "CONTINUE"
                      }
                    }
                    positional_constraint = "CONTAINS"
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
              }
            }
            # POST /v1/stress (requestid + uuid + length)
            statement {
              and_statement {
                statement {
                  byte_match_statement {
                    search_string         = "/v1/stress"
                    field_to_match {
                      uri_path {}
                    }
                    positional_constraint = "EXACTLY"
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
                statement {
                  regex_match_statement {
                    regex_string = "^[0-9]+$"
                    field_to_match {
                      json_body {
                        match_pattern {
                          included_paths = ["/requestid"]
                        }
                        match_scope             = "ALL"
                        invalid_fallback_behavior = "NO_MATCH"
                        oversize_handling       = "CONTINUE"
                      }
                    }
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
                statement {
                  regex_match_statement {
                    regex_string = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
                    field_to_match {
                      json_body {
                        match_pattern {
                          included_paths = ["/uuid"]
                        }
                        match_scope             = "ALL"
                        invalid_fallback_behavior = "NO_MATCH"
                        oversize_handling       = "CONTINUE"
                      }
                    }
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
                statement {
                  byte_match_statement {
                    search_string         = "length"
                    field_to_match {
                      body {
                        oversize_handling = "CONTINUE"
                      }
                    }
                    positional_constraint = "CONTAINS"
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
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

  # Rule 3: ALLOW valid PUT /v1/product
  rule {
    name     = "AllowValidPUT"
    priority = 3
    action {
      allow {}
    }
    statement {
      and_statement {
        statement {
          byte_match_statement {
            search_string         = "PUT"
            field_to_match {
              method {}
            }
            positional_constraint = "EXACTLY"
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          byte_match_statement {
            search_string         = "/v1/product"
            field_to_match {
              uri_path {}
            }
            positional_constraint = "EXACTLY"
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          byte_match_statement {
            search_string         = "requestid"
            field_to_match {
              body {
                oversize_handling = "CONTINUE"
              }
            }
            positional_constraint = "CONTAINS"
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          byte_match_statement {
            search_string         = "uuid"
            field_to_match {
              body {
                oversize_handling = "CONTINUE"
              }
            }
            positional_constraint = "CONTAINS"
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
      }
    }
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "AllowValidPUT"
    }
  }

  visibility_config {
    sampled_requests_enabled   = true
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name}-cf-acl"
  }

  tags = { Name = "${local.name}-cf-acl" }
}


# WAF 로그 → CloudWatch Logs (us-east-1, CloudFront WAF 요구사항)
resource "aws_cloudwatch_log_group" "waf" {
  provider          = aws.us_east_1
  name              = "aws-waf-logs-${local.name}"
  retention_in_days = 7
}

resource "aws_wafv2_web_acl_logging_configuration" "cloudfront" {
  provider                = aws.us_east_1
  log_destination_configs = [aws_cloudwatch_log_group.waf.arn]
  resource_arn            = aws_wafv2_web_acl.cloudfront.arn
}
