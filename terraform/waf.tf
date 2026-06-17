# WAF WebACL - REGIONAL, associated with terraform-managed ALB
resource "aws_wafv2_web_acl" "regional" {
  name  = "${local.name}-acl"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  # Rule 10: AllowValidGET (from tools/waf/get.json logic)
  rule {
    name     = "AllowValidGET"
    priority = 10
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

  # Rule 11: AllowValidPOST (from tools/waf/post.json logic)
  rule {
    name     = "AllowValidPOST"
    priority = 11
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
          }
        }
        statement {
          or_statement {
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

  # Rule 1: BlockSQLInjection (from tools/waf/block_sqli.json)
  rule {
    name     = "BlockSQLInjection"
    priority = 1
    action {
      block {}
    }
    statement {
      or_statement {
        statement {
          sqli_match_statement {
            field_to_match {
              query_string {}
            }
            text_transformation {
              priority = 0
              type     = "URL_DECODE"
            }
            text_transformation {
              priority = 1
              type     = "LOWERCASE"
            }
          }
        }
        statement {
          sqli_match_statement {
            field_to_match {
              body {
                oversize_handling = "CONTINUE"
              }
            }
            text_transformation {
              priority = 0
              type     = "URL_DECODE"
            }
            text_transformation {
              priority = 1
              type     = "LOWERCASE"
            }
          }
        }
        statement {
          sqli_match_statement {
            field_to_match {
              uri_path {}
            }
            text_transformation {
              priority = 0
              type     = "URL_DECODE"
            }
            text_transformation {
              priority = 1
              type     = "LOWERCASE"
            }
          }
        }
      }
    }
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "BlockSQLInjection"
    }
  }

  # Rule 2: BlockXSS (from tools/waf/block_xss.json)
  rule {
    name     = "BlockXSS"
    priority = 2
    action {
      block {}
    }
    statement {
      or_statement {
        statement {
          xss_match_statement {
            field_to_match {
              query_string {}
            }
            text_transformation {
              priority = 0
              type     = "URL_DECODE"
            }
            text_transformation {
              priority = 1
              type     = "HTML_ENTITY_DECODE"
            }
          }
        }
        statement {
          xss_match_statement {
            field_to_match {
              body {
                oversize_handling = "CONTINUE"
              }
            }
            text_transformation {
              priority = 0
              type     = "URL_DECODE"
            }
            text_transformation {
              priority = 1
              type     = "HTML_ENTITY_DECODE"
            }
          }
        }
        statement {
          xss_match_statement {
            field_to_match {
              uri_path {}
            }
            text_transformation {
              priority = 0
              type     = "URL_DECODE"
            }
            text_transformation {
              priority = 1
              type     = "HTML_ENTITY_DECODE"
            }
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

  # Rule 3: BlockScanner (from tools/waf/block_scanner.json)
  rule {
    name     = "BlockScanner"
    priority = 3
    action {
      block {}
    }
    statement {
      or_statement {
        statement {
          regex_match_statement {
            regex_string = "(nikto|sqlmap|nmap|masscan|dirbuster|gobuster|wfuzz|hydra|burpsuite|nessus|acunetix|zgrab|nuclei|feroxbuster)"
            field_to_match {
              single_header {
                name = "user-agent"
              }
            }
            text_transformation {
              priority = 0
              type     = "LOWERCASE"
            }
          }
        }
        statement {
          size_constraint_statement {
            field_to_match {
              single_header {
                name = "user-agent"
              }
            }
            comparison_operator = "EQ"
            size                = 0
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          regex_match_statement {
            regex_string = "(\\.\\./|/etc/passwd|/proc/self|\\.env|\\.git|wp-admin|phpmyadmin|/actuator|/swagger)"
            field_to_match {
              uri_path {}
            }
            text_transformation {
              priority = 0
              type     = "URL_DECODE"
            }
          }
        }
      }
    }
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "BlockScanner"
    }
  }

  visibility_config {
    sampled_requests_enabled   = true
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name}-acl"
  }

  tags = { Name = "${local.name}-acl" }
}

resource "aws_wafv2_web_acl_association" "alb" {
  resource_arn = aws_lb.this.arn
  web_acl_arn  = aws_wafv2_web_acl.regional.arn
}
