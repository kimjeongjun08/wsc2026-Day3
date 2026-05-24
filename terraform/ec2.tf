# EC2 Security Group
resource "aws_security_group" "ec2" {
  name   = "apdev-ec2-sg"
  vpc_id = aws_vpc.main.id

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "apdev-ec2-sg" }
}

# IAM Role for EC2
resource "aws_iam_role" "ec2" {
  name = "apdev-ec2-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "admin" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

resource "aws_iam_instance_profile" "ec2" {
  name = "apdev-ec2-profile"
  role = aws_iam_role.ec2.name
}

# EC2 Instance (matches bastion spec)
resource "aws_instance" "app" {
  ami                         = "ami-010502f62836f0c67"
  instance_type               = "t3.small"
  subnet_id                   = aws_subnet.public_a.id
  vpc_security_group_ids      = [aws_security_group.ec2.id]
  iam_instance_profile        = aws_iam_instance_profile.ec2.name
  associate_public_ip_address = true

  tags = { Name = "apdev-ec2" }

  depends_on = [
    aws_db_proxy_target.main,
    aws_ecr_repository.user,
    aws_ecr_repository.product,
    aws_ecr_repository.stress,
    aws_s3_object.user_bin,
    aws_s3_object.product_bin,
    aws_s3_object.stress_bin,
  ]
}
