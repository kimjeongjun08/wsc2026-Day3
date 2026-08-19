resource "aws_secretsmanager_secret" "db" {
  name                    = "${local.name}-rds-secret"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    username = "admin"
    password = "Skill53##"
    host     = aws_db_instance.this.address
    port     = aws_db_instance.this.port
    dbname   = var.db_name
  })
}

resource "aws_security_group" "proxy" {
  name   = "${local.name}-proxy-sg"
  vpc_id = aws_vpc.this.id

  ingress {
    from_port   = 3306
    to_port     = 3306
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-proxy-sg" }
}

resource "aws_iam_role" "rds_proxy" {
  name = "${local.name}-rds-proxy-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "rds.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "rds_proxy" {
  name = "${local.name}-rds-proxy-secrets"
  role = aws_iam_role.rds_proxy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
      Resource = [aws_secretsmanager_secret.db.arn]
    }]
  })
}

resource "aws_db_proxy" "this" {
  name                   = "${local.name}-proxy"
  engine_family          = "MYSQL"
  role_arn               = aws_iam_role.rds_proxy.arn
  vpc_subnet_ids         = aws_subnet.public[*].id
  vpc_security_group_ids = [aws_security_group.proxy.id]
  require_tls            = false

  auth {
    auth_scheme               = "SECRETS"
    iam_auth                  = "DISABLED"
    secret_arn                = aws_secretsmanager_secret.db.arn
    client_password_auth_type = "MYSQL_NATIVE_PASSWORD"
  }

  tags = { Name = "${local.name}-proxy" }
}

resource "aws_db_proxy_default_target_group" "this" {
  db_proxy_name = aws_db_proxy.this.name

  connection_pool_config {
    # ★백엔드 커넥션 상한 = DB 의 max_connections × 이 비율.
    #   rds.tf 에서 max_connections 를 150 으로 올렸으므로 여기서 100% 를 주면 150 이 된다.
    #   90 → 100 자체의 효과는 작지만(24→27), max_connections 수정과 짝이라 같이 올린다.
    max_connections_percent = 100
    # ★유휴 커넥션을 넉넉히 유지한다. 낮으면 스파이크 때 매번 새로 맺느라 borrow 가 늦다.
    max_idle_connections_percent = 90
    connection_borrow_timeout    = 15
  }
}

resource "aws_db_proxy_target" "this" {
  db_proxy_name          = aws_db_proxy.this.name
  target_group_name      = aws_db_proxy_default_target_group.this.name
  db_instance_identifier = aws_db_instance.this.identifier
}
