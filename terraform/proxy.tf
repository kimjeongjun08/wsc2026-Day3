# Secrets Manager - DB credentials for RDS Proxy
resource "aws_secretsmanager_secret" "db" {
  name = "apdev-rds-secret"
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    username = var.db_username
    password = var.db_password
    engine   = "mysql"
    host     = aws_db_instance.mysql.address
    port     = 3306
    dbname   = var.db_name
  })
}

# RDS Proxy Security Group
resource "aws_security_group" "proxy" {
  name   = "apdev-proxy-sg"
  vpc_id = aws_vpc.main.id

  ingress {
    from_port       = 3306
    to_port         = 3306
    protocol        = "tcp"
    security_groups = [aws_security_group.ec2.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "apdev-proxy-sg" }
}

# IAM Role for RDS Proxy
resource "aws_iam_role" "proxy" {
  name = "apdev-rds-proxy-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = { Service = "rds.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "proxy" {
  name = "apdev-rds-proxy-policy"
  role = aws_iam_role.proxy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue", "secretsmanager:GetResourcePolicy", "secretsmanager:DescribeSecret", "secretsmanager:ListSecretVersionIds"]
      Resource = [aws_secretsmanager_secret.db.arn]
    }]
  })
}

# RDS Proxy
resource "aws_db_proxy" "main" {
  name                   = "apdev-rds-proxy"
  debug_logging          = false
  engine_family          = "MYSQL"
  idle_client_timeout    = 1800
  require_tls            = false
  role_arn               = aws_iam_role.proxy.arn
  vpc_security_group_ids = [aws_security_group.proxy.id]
  vpc_subnet_ids         = [aws_subnet.private_a.id, aws_subnet.private_b.id]

  auth {
    auth_scheme                = "SECRETS"
    iam_auth                   = "DISABLED"
    client_password_auth_type  = "MYSQL_NATIVE_PASSWORD"
    secret_arn                 = aws_secretsmanager_secret.db.arn
  }

  tags = { Name = "apdev-rds-proxy" }
}

resource "aws_db_proxy_default_target_group" "main" {
  db_proxy_name = aws_db_proxy.main.name

  connection_pool_config {
    max_connections_percent = 100
  }
}

resource "aws_db_proxy_target" "main" {
  db_proxy_name          = aws_db_proxy.main.name
  target_group_name      = aws_db_proxy_default_target_group.main.name
  db_instance_identifier = aws_db_instance.mysql.identifier
}
