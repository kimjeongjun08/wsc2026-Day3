resource "aws_db_subnet_group" "this" {
  name       = "${local.name}-db-subnets"
  subnet_ids = aws_subnet.public[*].id
  tags       = { Name = "${local.name}-db-subnets" }
}

resource "aws_security_group" "rds" {
  name        = "${local.name}-rds-sg"
  description = "RDS access from EKS nodes"
  vpc_id      = aws_vpc.this.id

  ingress {
    from_port   = 3306
    to_port     = 3306
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
    description = "MySQL from anywhere in VPC (EKS nodes use cluster-managed SG)"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Custom parameter group: tuned for read-heavy product GETs and many short connections
resource "aws_db_parameter_group" "mysql8" {
  name        = "${local.name}-mysql8"
  family      = "mysql8.0"
  description = "Tuned for ${local.name}"

  parameter {
    name  = "character_set_server"
    value = "utf8mb4"
  }
  parameter {
    name  = "collation_server"
    value = "utf8mb4_unicode_ci"
  }
  parameter {
    name  = "time_zone"
    value = "Asia/Seoul"
  }
  parameter {
    name  = "max_connections"
    value = "300"
  }
  parameter {
    name  = "slow_query_log"
    value = "1"
  }
  parameter {
    name  = "long_query_time"
    value = "1"
  }
}

resource "aws_db_instance" "this" {
  identifier        = "${local.name}-rds"
  engine            = "mysql"
  engine_version    = "8.0"
  instance_class    = var.db_instance_class
  allocated_storage     = 20    # 연습용 최소값. 대회 당일: 65536 (최대 64TB for gp3)
  max_allocated_storage = 30    # 연습용. 대회 당일: 65536
  storage_type          = "gp3"
  multi_az          = true

  db_name  = var.db_name
  username = "admin"
  password = "Skill53##"
  port     = 3306

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.mysql8.name

  backup_retention_period   = 1
  backup_window             = "17:00-18:00"
  maintenance_window        = "sun:18:30-sun:19:30"
  skip_final_snapshot       = true
  apply_immediately         = true
  publicly_accessible       = false
  storage_encrypted         = true
  monitoring_interval       = 60
  monitoring_role_arn       = aws_iam_role.rds_monitoring.arn
  enabled_cloudwatch_logs_exports = ["error", "slowquery"]
  auto_minor_version_upgrade      = false
  deletion_protection             = false

  tags = { Name = "${local.name}-rds" }
}

resource "aws_iam_role" "rds_monitoring" {
  name = "${local.name}-rds-monitoring"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "monitoring.rds.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}
