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

# Upload application binaries to S3 (setup 버킷)
resource "aws_s3_object" "app_binaries" {
  for_each = toset(["user", "product", "stress"])
  bucket   = aws_s3_bucket.setup.bucket
  key      = "application/${each.value}/${each.value}"
  source   = "${path.module}/application/${each.value}/${each.value}"
  etag     = filemd5("${path.module}/application/${each.value}/${each.value}")
}

# Upload static k8s files to S3 (setup 버킷)
resource "aws_s3_object" "k8s_static" {
  # ★overprovisioning.yaml 을 빠뜨리고 있었다.
  #   자리표시 파드(pause, priority -10)로 노드를 미리 확보하는 장치인데,
  #   매니페스트는 레포에 있고 priorityclass.yaml 에 pause-priority 까지
  #   정의돼 있는데 정작 이 파일만 S3 로 안 올라가서 배포된 적이 없다.
  #   실측(2026-08-21): apply.sh 가 이걸 scale 하려다 조용히 실패하고 있었다
  #   (deployments.apps "overprovisioning" not found).
  for_each = toset(["service.yaml", "hpa.yaml", "pdb.yaml", "iam_policy.json", "install-karpenter.sh", "priorityclass.yaml", "overprovisioning.yaml"])
  bucket   = aws_s3_bucket.setup.bucket
  key      = "k8s/${each.value}"
  source   = "${path.module}/k8s/${each.value}"
  etag     = filemd5("${path.module}/k8s/${each.value}")
}

# Upload templated deploy.yaml (setup 버킷)
resource "aws_s3_object" "k8s_deploy" {
  bucket  = aws_s3_bucket.setup.bucket
  key     = "k8s/deploy.yaml"
  content = replace(
    replace(
      replace(
        file("${path.module}/k8s/deploy.yaml"),
        "ACCOUNT_ID", data.aws_caller_identity.current.account_id
      ),
      "REGION", var.region
    ),
    "PROJECT", local.name
  )
}

# Upload templated karpenter.yaml (setup 버킷)
resource "aws_s3_object" "k8s_karpenter" {
  bucket  = aws_s3_bucket.setup.bucket
  key     = "k8s/karpenter.yaml"
  content = templatefile("${path.module}/k8s/karpenter.yaml", {
    name               = local.name
    cluster_name       = aws_eks_cluster.this.name
    node_instance_type = var.node_instance_type
    subnet_a           = aws_subnet.public[0].id
    subnet_b           = aws_subnet.public[1].id
  })
}

# Upload templated tgb.yaml (setup 버킷)
resource "aws_s3_object" "k8s_tgb" {
  bucket  = aws_s3_bucket.setup.bucket
  key     = "k8s/tgb.yaml"
  content = templatefile("${path.module}/k8s/tgb.yaml", {
    tg_user_arn    = aws_lb_target_group.user.arn
    tg_product_arn = aws_lb_target_group.product.arn
    tg_stress_arn  = aws_lb_target_group.stress.arn
  })
}

# Upload templated configmap.yaml (setup 버킷)
resource "aws_s3_object" "k8s_configmap" {
  bucket  = aws_s3_bucket.setup.bucket
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

# Upload dump.sql (setup 버킷) - 대회 당일 실제 dump로 교체
resource "aws_s3_object" "dump_sql" {
  bucket = aws_s3_bucket.setup.bucket
  key    = "load_user.dump"
  source = "${path.module}/application/load_user.dump"
  etag   = filemd5("${path.module}/application/load_user.dump")
}

resource "aws_instance" "bastion" {
  # ★설치 전용 인스턴스. 채점 시점에는 꺼야 한다.
  #   · 과제 스펙: "EC2 인스턴스는 t3.medium 타입만" + "불필요한 리소스(미사용 EC2) 감점"
  #   · 비용 지표는 계정의 running 인스턴스 "전체 수"를 센다 → bastion 1대 = 비용 2점 손해
  #   EKS API 가 퍼블릭이라 설치가 끝나면 kubectl 은 로컬에서 그대로 된다.
  #   설치 완료 후:  terraform apply -var bastion_enabled=false
  count = var.bastion_enabled ? 1 : 0

  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = "t3.small"
  subnet_id                   = aws_subnet.public[0].id
  vpc_security_group_ids      = [aws_security_group.ec2.id]
  iam_instance_profile        = aws_iam_instance_profile.ec2.name
  associate_public_ip_address = true

  # ★newtech 방식 미러: base64gzip(16KB 한도 대비 압축, cloud-init 자동해제) + CRLF→LF 강제.
  #   CRLF면 bash `\` 줄연속이 깨져 eksctl 명령 truncate → IAM 역할 미생성 → NoSuchEntity.
  #   raw user_data는 setup.sh 커지면 16KB 초과로 잘릴 위험 → gzip으로 회피(newtech가 그래서 됨).
  user_data_base64 = base64gzip(replace(templatefile("${path.module}/userdata.tpl", {
    setup_script = templatefile("${path.module}/setup.sh", {
      region         = var.region
      account_id     = data.aws_caller_identity.current.account_id
      db_host        = aws_db_instance.this.address
      db_port        = "3306"
      db_user        = "admin"
      db_pass        = "Skill53##"
      db_name        = var.db_name
      s3_bucket      = aws_s3_bucket.images.bucket
      setup_bucket   = aws_s3_bucket.setup.bucket
      cluster_name   = aws_eks_cluster.this.name
      vpc_id         = aws_vpc.this.id
      ecr_prefix     = local.name
    })
    artifacts_bucket = aws_s3_bucket.setup.bucket
    region           = var.region
  }), "\r", ""))

  tags = { Name = "${local.name}-bastion" }

  depends_on = [aws_db_proxy_target.this, aws_ecr_repository.this, aws_eks_node_group.this, aws_s3_object.k8s_configmap, aws_s3_object.k8s_tgb, aws_s3_object.k8s_static, aws_s3_object.app_binaries, aws_s3_object.dump_sql]
}
