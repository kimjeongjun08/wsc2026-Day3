# RDS Security Group
resource "aws_security_group" "rds" {
  name   = "apdev-rds-sg"
  vpc_id = aws_vpc.main.id

  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "apdev-rds-sg" }
}

# DB Subnet Group
resource "aws_db_subnet_group" "main" {
  name       = "apdev-db-subnet"
  subnet_ids = [aws_subnet.private_a.id, aws_subnet.private_b.id]
  tags       = { Name = "apdev-db-subnet" }
}

# RDS Parameter Group
resource "aws_db_parameter_group" "mysql" {
  name   = "apdev-mysql-params"
  family = "mysql8.0"

  parameter {
    name  = "default_authentication_plugin"
    value = "mysql_native_password"
  }

  parameter {
    name  = "max_connections"
    value = "500"
  }

  parameter {
    name  = "innodb_buffer_pool_size"
    value = "{DBInstanceClassMemory*3/4}"
  }

  parameter {
    name  = "slow_query_log"
    value = "1"
  }

  parameter {
    name  = "long_query_time"
    value = "1"
  }

  parameter {
    name  = "performance_schema"
    value = "1"
  }
}

# RDS MySQL 8.0 Multi-AZ
resource "aws_db_instance" "mysql" {
  identifier              = "apdev-rds-instance"
  engine                  = "mysql"
  engine_version          = "8.0"
  instance_class          = "db.t3.micro"
  allocated_storage       = 100
  max_allocated_storage   = 200
  storage_type            = "gp3"
  iops                    = 3000
  storage_throughput      = 125
  multi_az                = true
  db_name                 = "dev"
  username                = var.db_username
  password                = var.db_password
  parameter_group_name    = aws_db_parameter_group.mysql.name
  db_subnet_group_name    = aws_db_subnet_group.main.name
  vpc_security_group_ids  = [aws_security_group.rds.id]
  skip_final_snapshot     = true
  deletion_protection     = false
  backup_retention_period = 1
  monitoring_interval     = 60
  monitoring_role_arn     = aws_iam_role.rds_monitoring.arn

  tags = { Name = "apdev-rds-instance" }
}

# RDS Enhanced Monitoring Role
resource "aws_iam_role" "rds_monitoring" {
  name = "apdev-rds-monitoring-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "monitoring.rds.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}
