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

# Parameter group: charset/locale/slow query만, max_connections는 AWS 기본값 사용
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
    name  = "slow_query_log"
    value = "1"
  }
  parameter {
    name  = "long_query_time"
    value = "0.5"
  }
  # ★max_connections 는 기본 공식 {DBInstanceClassMemory/12582880} 을 그대로 쓴다.
  #   db.t3.micro 에서는 27 밖에 안 나오고 RDS Proxy 백엔드 상한이 24 로 잡히지만,
  #   150 으로 올려(백엔드 72) 재측정한 결과 점수·지연이 전혀 변하지 않았다 —
  #   x1.0 스파이크: user perf 74.28% → 73.94%, up50 165ms → 165ms (오차 범위).
  #   BorrowLatency 70~80ms 도 그대로였다. 즉 커넥션 개수는 병목이 아니다.
  #   올리면 FreeableMemory 가 51MB 까지 떨어져 손해만 본다.
}

resource "aws_db_instance" "this" {
  identifier        = "apdev-rds-instance"
  engine            = "mysql"
  engine_version    = "8.0"
  instance_class        = var.db_instance_class
  allocated_storage     = var.db_allocated_storage
  storage_type          = "gp3"
  # ★iops / storage_throughput 은 명시하지 않는다 = gp3 기준선(추가 과금 없음).
  #   < 400 GiB :  3,000 IOPS / 125 MiB/s     >= 400 GiB: 12,000 IOPS / 500 MiB/s
  #   기준선을 넘겨 프로비저닝하면 별도 과금되고 Multi-AZ 라 2배로 붙는다(시간당 $2~3 규모).
  #   그리고 db.t3.micro 는 그만큼 쓸 수 있는 인스턴스가 아니다 —
  #   실측 최대 WriteIOPS 1,573 / ReadIOPS 35 로 기준선의 일부만 쓴다.
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

  tags = { Name = "apdev-rds-instance" }
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
