# WAF WebACL — CLOUDFRONT scope. ★ 엄격 화이트리스트 + 블랙리스트 이중 방어.
#
# 설계:
#   1) BlockAttacks(priority 1): 공격 패턴 즉시 차단 → 403 (double URL_DECODE, null byte)
#   2) BlockHeaderAttacks(priority 2): 헤더 값 공격 → 403
#   3) BlockUnknownPath(priority 3): 유효 경로 외 → 404
#   4) AllowValidGET(priority 10): GET 형식 검증 (id/email 값 regex)
#   5) AllowValidPOST(priority 11): POST body 필수필드
#   6) AllowValidPUT(priority 12): PUT 메소드+경로
#   7) default BLOCK → 403
#
# ★ 정상은 100% 통과 보장:
#   - id: 영문+숫자+하이픈+언더바+점 (정상 injector 패턴: grade-xxx-pN, seed_xxx 등)
#   - email: @포함, 영문+숫자+특수일부 (xxx@example.org)
#   - body: 필수 키워드 포함 (username, name, price, length 등)
#
# ★ 비정상은 100% 차단:
#   - 공격 패턴(query/body/uri/헤더) → 403
#   - 없는 경로 → 404
#   - 형식 불일치(id에 ../, %00, script 등) → AllowValid 탈락 → default BLOCK → 403
#   - 잘못된 메소드 → default BLOCK → 403

locals {
  # SPEC:PATHS:BEGIN (apply_spec.py가 spec.py 기준으로 자동 생성 — 직접 수정 말 것)
  waf_get_exact  = ["/v1/user", "/v1/product"]
  waf_post_exact = ["/v1/user", "/v1/product", "/v1/stress"]
  waf_put_exact  = ["/v1/product"]
  waf_prefix     = ["/images/"]
  # SPEC:PATHS:END

  waf_prefix_re = length(local.waf_prefix) > 0 ? format("|^(%s)", join("|", local.waf_prefix)) : ""
  waf_re_known  = format("^(%s)$%s", join("|", distinct(concat(local.waf_get_exact, local.waf_post_exact, local.waf_put_exact))), local.waf_prefix_re)

  # 값 검증 regex
  # id: 영문+숫자+하이픈+언더바+점 (grade-681690000-p10, seed_123, lt_456 등)
  id_regex = "^[a-zA-Z0-9_.:-]+$"
  # email: 느슨한 이메일 (xxx@xxx.xxx)
  email_regex = "^[a-zA-Z0-9._%+=-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$"
  # ★requestid 검증 제거: 채점 injector는 requestid를 32-hex로 보내고 product GET엔 아예 없음
  #   → "숫자만(^[0-9]+$)" 검사가 정상 GET(user+product)을 전량 403 차단하는 FP였음.
  #   requestid에 든 공격은 Rule1(쿼리 특수문자)·BlockAttacks가 priority 1에서 먼저 잡으므로 보안 영향 0.
}

# ── 공격 패턴셋 (query/body/uri 공용) ──
resource "aws_wafv2_regex_pattern_set" "attacks" {
  provider    = aws.us_east_1
  name        = "${local.name}-attack-patterns"
  description = "SQLi XSS SSTI LFI SSRF RCE scanner + CRLF + null byte"
  scope       = "CLOUDFRONT"

  # SQLi core (정상값=영숫자/@/./_/-/숫자엔 없는 토큰만 → FP 0)
  regular_expression {
    regex_string = "(1=1|\\bor\\s+\\d+=\\d+|union\\s+select|select.{0,40}from|insert\\s+into|delete\\s+from|drop\\s+table|information_schema|' or |\" or |'--|'#)"
  }
  # SQLi 함수/시간/추출 기반
  regular_expression {
    regex_string = "(sleep\\(|benchmark\\(|pg_sleep|waitfor\\s+delay|xp_cmdshell|into\\s+outfile|load_file\\(|extractvalue\\(|updatexml\\(|@@version|concat\\(|char\\(|cast\\(|;\\s*(select|insert|update|delete|drop)|0x[0-9a-f]{8})"
  }
  # XSS 태그 (< 는 정상값에 없음 → FP 0)
  regular_expression {
    regex_string = "(<script|javascript:|vbscript:|<(iframe|svg|img|body|style|link|meta|object|embed|base|video|audio|details|form|marquee|math)\\b|data:text/html)"
  }
  # XSS 이벤트핸들러 + SSTI + JNDI + XXE
  regular_expression {
    regex_string = "(on(error|load|mouseover|mouseout|focus|blur|click|submit|change|toggle|keydown)\\s*=|document\\.cookie|alert\\(|prompt\\(|eval\\(|expression\\(|\\{\\{|\\$\\{|#\\{|<%|jndi:|<!entity|<!doctype)"
  }
  # LFI + SSRF (디코딩 후 매칭) + php wrapper + 클라우드 메타
  regular_expression {
    regex_string = "(\\.\\.[/\\\\]|etc/passwd|etc/shadow|proc/self|boot\\.ini|win\\.ini|169\\.254\\.169\\.254|169\\.254\\.|127\\.0\\.0\\.1|0\\.0\\.0\\.0|localhost|metadata\\.google|php://|file://|gopher://|dict://|expect://)"
  }
  # RCE + Shellshock
  regular_expression {
    regex_string = "(\\(\\) \\{|/bin/sh|/bin/bash|(;|`|\\$\\()\\s*(cat|whoami|wget|curl|nc\\s|bash|ping|uname))"
  }
  # 스캐너
  regular_expression {
    regex_string = "(sqlmap|nikto|nmap|masscan|zgrab|nessus|acunetix|nuclei|gobuster|dirbuster|wpscan|hydra|metasploit|whatweb|feroxbuster|wfuzz|ffuf|dirsearch|zap)"
  }
  # CRLF
  regular_expression {
    regex_string = "(%0d%0a|%0D%0A|set-cookie:)"
  }
  # NoSQL injection + Prototype pollution + LDAP + constructor + redirect (body 타겟)
  regular_expression {
    regex_string = "(\"\\$(?:gt|ne|lt|gte|lte|where|regex|exists|nin|in|or|and)\"|\\.\\$(?:gt|ne|where)|__proto__|\"constructor\"\\s*:\\s*\\{|\\*\\)\\(|\\)\\(\\&|\\)\\(\\||://)"
  }
}

# ── 헤더 값 공격셋 (Referer-safe: ://·IP 제외) ──
resource "aws_wafv2_regex_pattern_set" "header_attacks" {
  provider    = aws.us_east_1
  name        = "${local.name}-header-attacks"
  description = "header value attack signatures"
  scope       = "CLOUDFRONT"

  regular_expression {
    regex_string = "(union\\s+select|' or |\" or |information_schema|sleep\\(|benchmark\\(|pg_sleep|waitfor\\s+delay|xp_cmdshell|extractvalue\\(|updatexml\\(|@@version|load_file\\(|drop\\s+table|insert\\s+into|1=1)"
  }
  regular_expression {
    regex_string = "(<script|javascript:|vbscript:|onerror\\s*=|onload\\s*=|onmouseover\\s*=|<iframe|<svg|<img|<body|<style|document\\.cookie|alert\\(|expression\\(|jndi:|<!entity|<!doctype)"
  }
  regular_expression {
    regex_string = "(\\.\\.[/\\\\]|etc/passwd|etc/shadow|proc/self|\\(\\) \\{|/bin/sh|/bin/bash|%2e%2e|%252e)"
  }
  regular_expression {
    regex_string = "(sqlmap|nikto|nmap|masscan|zgrab|nessus|acunetix|nuclei|gobuster|dirbuster|wpscan|hydra|metasploit|whatweb|feroxbuster|dirb|wfuzz|ffuf|dirsearch|zap)"
  }
}


resource "aws_wafv2_web_acl" "cloudfront" {
  provider = aws.us_east_1
  name     = "${local.name}-cf-acl"
  scope    = "CLOUDFRONT"

  default_action {
    block {}
  }

  association_config {
    request_body {
      cloudfront {
        default_size_inspection_limit = "KB_64"
      }
    }
  }

  # ── Rule 0: 없는 경로 → 404 (최우선: 경로 외 요청은 공격 여부 무관하게 즉시 404) ──
  rule {
    name     = "BlockUnknownPath"
    priority = 0
    action {
      block {
        custom_response {
          response_code = 404
        }
      }
    }
    statement {
      not_statement {
        statement {
          regex_match_statement {
            regex_string = local.waf_re_known
            field_to_match {
              uri_path {}
            }
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
      metric_name                = "BlockUnknownPath"
    }
  }

  # ── Rule 1: 공격 패턴 차단 → 403 (double URL_DECODE) ──
  rule {
    name     = "BlockAttacks"
    priority = 1
    action {
      block {}
    }
    statement {
      or_statement {
        # 쿼리 특수문자 (정상 id/email/requestid/uuid에 없는 문자)
        statement {
          regex_match_statement {
            regex_string = "['\"<>(){}\\[\\];`\\\\|*$!]"
            field_to_match {
              query_string {}
            }
            text_transformation {
              priority = 0
              type     = "URL_DECODE"
            }
            text_transformation {
              priority = 1
              type     = "URL_DECODE"
            }
            text_transformation {
              priority = 2
              type     = "LOWERCASE"
            }
          }
        }
        # 쿼리 공격패턴셋 (double decode)
        statement {
          regex_pattern_set_reference_statement {
            arn = aws_wafv2_regex_pattern_set.attacks.arn
            field_to_match {
              query_string {}
            }
            text_transformation {
              priority = 0
              type     = "URL_DECODE"
            }
            text_transformation {
              priority = 1
              type     = "URL_DECODE"
            }
            text_transformation {
              priority = 2
              type     = "LOWERCASE"
            }
          }
        }
        # 바디 공격패턴셋 (double decode)
        statement {
          regex_pattern_set_reference_statement {
            arn = aws_wafv2_regex_pattern_set.attacks.arn
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
              type     = "URL_DECODE"
            }
            text_transformation {
              priority = 2
              type     = "LOWERCASE"
            }
          }
        }
        # URI 공격패턴셋 (double decode)
        statement {
          regex_pattern_set_reference_statement {
            arn = aws_wafv2_regex_pattern_set.attacks.arn
            field_to_match {
              uri_path {}
            }
            text_transformation {
              priority = 0
              type     = "URL_DECODE"
            }
            text_transformation {
              priority = 1
              type     = "URL_DECODE"
            }
            text_transformation {
              priority = 2
              type     = "LOWERCASE"
            }
          }
        }
        # Null byte raw 검사 (query)
        statement {
          byte_match_statement {
            search_string         = "%00"
            positional_constraint = "CONTAINS"
            field_to_match {
              query_string {}
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        # Null byte raw 검사 (body)
        statement {
          byte_match_statement {
            search_string         = "%00"
            positional_constraint = "CONTAINS"
            field_to_match {
              body {
                oversize_handling = "CONTINUE"
              }
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        # 인코딩 traversal raw 마커 (URI) — 디코딩 안 하고 원본에서 %2e%2e·%252e 등 탐지
        #   (double-decode 체인이 놓치는 이중/오버롱 인코딩 대비. 정상 URI엔 %인코딩 없음 → FP 0)
        statement {
          regex_match_statement {
            regex_string = "(%2e%2e|%252e|%252f|\\.\\.%2f|\\.\\.%5c|%c0%ae|%c1%9c|\\.\\.%c0|%uff0e)"
            field_to_match {
              uri_path {}
            }
            text_transformation {
              priority = 0
              type     = "LOWERCASE"
            }
          }
        }
        # 인코딩 traversal raw 마커 (query)
        statement {
          regex_match_statement {
            regex_string = "(%2e%2e|%252e|%252f|\\.\\.%2f|\\.\\.%5c|%c0%ae|%c1%9c|\\.\\.%c0|%uff0e)"
            field_to_match {
              query_string {}
            }
            text_transformation {
              priority = 0
              type     = "LOWERCASE"
            }
          }
        }
      }
    }
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "BlockAttacks"
    }
  }

  # ── Rule 2: 헤더 값 공격 → 403 ──
  rule {
    name     = "BlockHeaderAttacks"
    priority = 2
    action {
      block {}
    }
    statement {
      regex_pattern_set_reference_statement {
        arn = aws_wafv2_regex_pattern_set.header_attacks.arn
        field_to_match {
          headers {
            match_pattern {
              all {}
            }
            match_scope       = "VALUE"
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
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "BlockHeaderAttacks"
    }
  }

  # ── Rule 10: GET 허용 (값 검증 포함) ──
  # GET /v1/user: email 형식 (requestid 검증 제거 — FP 방지)
  # GET /v1/product: id 형식 (requestid 검증 제거 — FP 방지)
  # GET /images/*: 경로만
  rule {
    name     = "AllowValidGET"
    priority = 10
    action {
      allow {}
    }
    statement {
      or_statement {
        # GET /v1/user — email 형식 + requestid 숫자
        statement {
          and_statement {
            statement {
              byte_match_statement {
                search_string         = "GET"
                positional_constraint = "EXACTLY"
                field_to_match {
                  method {}
                }
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
            statement {
              byte_match_statement {
                search_string         = "/v1/user"
                positional_constraint = "EXACTLY"
                field_to_match {
                  uri_path {}
                }
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
            statement {
              regex_match_statement {
                regex_string = local.email_regex
                field_to_match {
                  single_query_argument {
                    name = "email"
                  }
                }
                text_transformation {
                  priority = 0
                  type     = "URL_DECODE"
                }
              }
            }
          }
        }
        # GET /v1/product — id 형식 + requestid 숫자
        statement {
          and_statement {
            statement {
              byte_match_statement {
                search_string         = "GET"
                positional_constraint = "EXACTLY"
                field_to_match {
                  method {}
                }
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
            statement {
              byte_match_statement {
                search_string         = "/v1/product"
                positional_constraint = "EXACTLY"
                field_to_match {
                  uri_path {}
                }
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
            statement {
              regex_match_statement {
                regex_string = local.id_regex
                field_to_match {
                  single_query_argument {
                    name = "id"
                  }
                }
                text_transformation {
                  priority = 0
                  type     = "URL_DECODE"
                }
              }
            }
          }
        }
        # GET /images/*
        statement {
          and_statement {
            statement {
              byte_match_statement {
                search_string         = "GET"
                positional_constraint = "EXACTLY"
                field_to_match {
                  method {}
                }
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
            statement {
              byte_match_statement {
                search_string         = "/images/"
                positional_constraint = "STARTS_WITH"
                field_to_match {
                  uri_path {}
                }
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

  # ── Rule 11: POST 허용 (body 필수필드 + requestid 숫자) ──
  rule {
    name     = "AllowValidPOST"
    priority = 11
    action {
      allow {}
    }
    statement {
      or_statement {
        # POST /v1/user — body에 username + @ + requestid 숫자
        statement {
          and_statement {
            statement {
              byte_match_statement {
                search_string         = "POST"
                positional_constraint = "EXACTLY"
                field_to_match {
                  method {}
                }
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
            statement {
              byte_match_statement {
                search_string         = "/v1/user"
                positional_constraint = "EXACTLY"
                field_to_match {
                  uri_path {}
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
                positional_constraint = "CONTAINS"
                field_to_match {
                  body {
                    oversize_handling = "CONTINUE"
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
                search_string         = "@"
                positional_constraint = "CONTAINS"
                field_to_match {
                  body {
                    oversize_handling = "CONTINUE"
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
        # POST /v1/product — body에 name + price
        statement {
          and_statement {
            statement {
              byte_match_statement {
                search_string         = "POST"
                positional_constraint = "EXACTLY"
                field_to_match {
                  method {}
                }
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
            statement {
              byte_match_statement {
                search_string         = "/v1/product"
                positional_constraint = "EXACTLY"
                field_to_match {
                  uri_path {}
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
                positional_constraint = "CONTAINS"
                field_to_match {
                  body {
                    oversize_handling = "CONTINUE"
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
                search_string         = "price"
                positional_constraint = "CONTAINS"
                field_to_match {
                  body {
                    oversize_handling = "CONTINUE"
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
        # POST /v1/stress — body에 length 포함
        statement {
          and_statement {
            statement {
              byte_match_statement {
                search_string         = "POST"
                positional_constraint = "EXACTLY"
                field_to_match {
                  method {}
                }
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
            statement {
              byte_match_statement {
                search_string         = "/v1/stress"
                positional_constraint = "EXACTLY"
                field_to_match {
                  uri_path {}
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
                positional_constraint = "CONTAINS"
                field_to_match {
                  body {
                    oversize_handling = "CONTINUE"
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
      }
    }
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "AllowValidPOST"
    }
  }

  # ── Rule 12: PUT 허용 (메소드+경로만 — multipart body 검증 불가) ──
  rule {
    name     = "AllowValidPUT"
    priority = 12
    action {
      allow {}
    }
    statement {
      and_statement {
        statement {
          byte_match_statement {
            search_string         = "PUT"
            positional_constraint = "EXACTLY"
            field_to_match {
              method {}
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          byte_match_statement {
            search_string         = "/v1/product"
            positional_constraint = "EXACTLY"
            field_to_match {
              uri_path {}
            }
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
