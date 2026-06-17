data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_iam_role" "ec2" {
  name = "${local.name}-ec2-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ec2_admin" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

resource "aws_iam_instance_profile" "ec2" {
  name = "${local.name}-ec2-profile"
  role = aws_iam_role.ec2.name
}

resource "aws_security_group" "ec2" {
  name   = "${local.name}-ec2-sg"
  vpc_id = aws_vpc.this.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-ec2-sg" }
}

# Upload application binaries to S3
resource "aws_s3_object" "app_binaries" {
  for_each = toset(["user", "product", "stress"])
  bucket   = aws_s3_bucket.images.bucket
  key      = "application/${each.value}/${each.value}"
  source   = "${path.module}/application/${each.value}/${each.value}"
  etag     = filemd5("${path.module}/application/${each.value}/${each.value}")
}

# Upload static k8s files to S3
resource "aws_s3_object" "k8s_static" {
  for_each = toset(["service.yaml", "hpa.yaml", "pdb.yaml", "alb.sh", "iam_policy.json", "install-karpenter.sh"])
  bucket   = aws_s3_bucket.images.bucket
  key      = "k8s/${each.value}"
  source   = "${path.module}/k8s/${each.value}"
  etag     = filemd5("${path.module}/k8s/${each.value}")
}

# Upload templated deploy.yaml (with correct ECR image URIs)
resource "aws_s3_object" "k8s_deploy" {
  bucket  = aws_s3_bucket.images.bucket
  key     = "k8s/deploy.yaml"
  content = replace(
    replace(
      replace(
        file("${path.module}/k8s/deploy.yaml"),
        "003150130236.dkr.ecr.ap-northeast-2.amazonaws.com/apdev-user:latest",
        "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com/${local.name}-user:latest"
      ),
      "003150130236.dkr.ecr.ap-northeast-2.amazonaws.com/apdev-product:latest",
      "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com/${local.name}-product:latest"
    ),
    "003150130236.dkr.ecr.ap-northeast-2.amazonaws.com/apdev-stress:latest",
    "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com/${local.name}-stress:latest"
  )
}

# Upload templated eksctl.yaml (with real subnet IDs)
resource "aws_s3_object" "k8s_eksctl" {
  bucket  = aws_s3_bucket.images.bucket
  key     = "k8s/eksctl.yaml"
  content = templatefile("${path.module}/k8s/eksctl.yaml", {
    cluster_name       = "${local.name}-cluster"
    region             = var.region
    az_a               = var.azs[0]
    az_b               = var.azs[1]
    subnet_a           = aws_subnet.public[0].id
    subnet_b           = aws_subnet.public[1].id
    node_instance_type = var.node_instance_type
  })
}

# Upload templated karpenter.yaml
resource "aws_s3_object" "k8s_karpenter" {
  bucket  = aws_s3_bucket.images.bucket
  key     = "k8s/karpenter.yaml"
  content = templatefile("${path.module}/k8s/karpenter.yaml", {
    name               = local.name
    cluster_name       = "${local.name}-cluster"
    node_instance_type = var.node_instance_type
    subnet_a           = aws_subnet.public[0].id
    subnet_b           = aws_subnet.public[1].id
  })
}

# Upload templated configmap.yaml (with real DB/S3 values)
resource "aws_s3_object" "k8s_configmap" {
  bucket  = aws_s3_bucket.images.bucket
  key     = "k8s/configmap.yaml"
  content = templatefile("${path.module}/k8s/configmap.yaml", {
    db_host   = aws_db_proxy.this.endpoint
    db_port   = "3306"
    db_user   = "admin"
    db_pass   = "Skill53##"
    db_name   = var.db_name
    s3_bucket = aws_s3_bucket.images.bucket
    region    = var.region
  })
}

resource "aws_instance" "bastion" {
  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = "t3.small"
  subnet_id                   = aws_subnet.public[0].id
  vpc_security_group_ids      = [aws_security_group.ec2.id]
  iam_instance_profile        = aws_iam_instance_profile.ec2.name
  associate_public_ip_address = true

  user_data = templatefile("${path.module}/userdata.tpl", {
    setup_script = templatefile("${path.module}/setup.sh", {
      region       = var.region
      account_id   = data.aws_caller_identity.current.account_id
      db_host      = aws_db_instance.this.address
      db_port      = "3306"
      db_user      = "admin"
      db_pass      = "Skill53##"
      db_name      = var.db_name
      s3_bucket    = aws_s3_bucket.images.bucket
      cluster_name = "${local.name}-cluster"
      vpc_id       = aws_vpc.this.id
      subnet_ids   = join(",", aws_subnet.public[*].id)
      alb_sg_id    = aws_security_group.alb.id
      tg_user_arn  = aws_lb_target_group.user.arn
      tg_product_arn = aws_lb_target_group.product.arn
      tg_stress_arn  = aws_lb_target_group.stress.arn
      ecr_prefix     = local.name
    })
    artifacts_bucket = aws_s3_bucket.images.bucket
    region           = var.region
  })

  tags = { Name = "${local.name}-bastion" }

  depends_on = [aws_db_proxy_target.this, aws_ecr_repository.this, aws_s3_object.k8s_eksctl, aws_s3_object.k8s_configmap, aws_s3_object.k8s_static, aws_s3_object.app_binaries]
}
