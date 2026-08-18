resource "aws_security_group" "alb" {
  name   = "${local.name}-alb-sg"
  vpc_id = aws_vpc.this.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-alb-sg" }
}

resource "aws_lb" "this" {
  name               = "${local.name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  tags = { Name = "${local.name}-alb" }
}

resource "aws_lb_target_group" "user" {
  name                 = "${local.name}-user"
  port                 = 8080
  protocol             = "HTTP"
  vpc_id               = aws_vpc.this.id
  target_type          = "ip"
  deregistration_delay = 30
  # 처리 중 요청이 적은 타겟으로 라우팅 — 느린 요청에 물린 파드 회피
  load_balancing_algorithm_type = "least_outstanding_requests"

  health_check {
    path                = "/healthcheck"
    port                = "8080"
    healthy_threshold   = 2
    unhealthy_threshold = 2
    interval            = 5
    timeout             = 3
  }

  tags = { Name = "${local.name}-user" }
}

resource "aws_lb_target_group" "product" {
  name                          = "${local.name}-product"
  port                          = 8080
  protocol                      = "HTTP"
  vpc_id                        = aws_vpc.this.id
  target_type                   = "ip"
  deregistration_delay          = 30
  load_balancing_algorithm_type = "least_outstanding_requests"

  health_check {
    path                = "/healthcheck"
    port                = "8080"
    healthy_threshold   = 2
    unhealthy_threshold = 2
    interval            = 5
    timeout             = 3
  }

  tags = { Name = "${local.name}-product" }
}

resource "aws_lb_target_group" "stress" {
  name                 = "${local.name}-stress"
  port                 = 8080
  protocol             = "HTTP"
  vpc_id               = aws_vpc.this.id
  target_type          = "ip"
  deregistration_delay = 30
  # 핵심: length 큰 요청(수 초~수십 초)에 물린 파드로 새 요청이 가지 않게
  # → 가벼운 요청(SLO 통과 가능 클래스)이 무거운 요청 뒤에 줄 서는 것 방지
  load_balancing_algorithm_type = "least_outstanding_requests"

  health_check {
    path                = "/healthcheck"
    port                = "8080"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 5
    timeout             = 4
  }

  tags = { Name = "${local.name}-stress" }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "Not Found"
      status_code  = "404"
    }
  }
}

resource "aws_lb_listener_rule" "user" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.user.arn
  }

  condition {
    path_pattern { values = ["/v1/user*"] }
  }
}

resource "aws_lb_listener_rule" "product" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 20

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.product.arn
  }

  condition {
    path_pattern { values = ["/v1/product*"] }
  }
}

resource "aws_lb_listener_rule" "stress" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 30

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.stress.arn
  }

  condition {
    path_pattern { values = ["/v1/stress*"] }
  }
}
