# EKS Cluster Role (admin)
resource "aws_iam_role" "eks_cluster" {
  name = "${local.name}-eks-cluster-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "eks_cluster_admin" {
  role       = aws_iam_role.eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# EKS Node Role (admin)
resource "aws_iam_role" "eks_node" {
  name = "${local.name}-eks-node-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "eks_node_admin" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# EKS Security Group - all traffic open
resource "aws_security_group" "eks" {
  name   = "${local.name}-eks-sg"
  vpc_id = aws_vpc.this.id

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

  tags = { Name = "${local.name}-eks-sg" }
}

# EKS Cluster
resource "aws_eks_cluster" "this" {
  name     = "${local.name}-cluster"
  version  = var.eks_version
  role_arn = aws_iam_role.eks_cluster.arn

  vpc_config {
    subnet_ids              = aws_subnet.public[*].id
    security_group_ids      = [aws_security_group.eks.id]
    endpoint_public_access  = true
    endpoint_private_access = true
  }

  access_config {
    authentication_mode = "API_AND_CONFIG_MAP"
  }

  tags = { Name = "${local.name}-cluster" }
}

# EKS Access Entry - root
resource "aws_eks_access_entry" "root" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
}

resource "aws_eks_access_policy_association" "root" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope {
    type = "cluster"
  }
}

# EKS Access Entry - terraform 실행자 (윈도우에서 kubectl 접근용)
resource "aws_eks_access_entry" "caller" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = data.aws_caller_identity.current.arn
}

resource "aws_eks_access_policy_association" "caller" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = data.aws_caller_identity.current.arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope {
    type = "cluster"
  }
}

# EKS Access Entry - bastion role
resource "aws_eks_access_entry" "bastion" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_iam_role.ec2.arn
}

resource "aws_eks_access_policy_association" "bastion" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_iam_role.ec2.arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope {
    type = "cluster"
  }
}

# Metrics Server Addon
resource "aws_eks_addon" "metrics_server" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "metrics-server"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
}

# ★MNG용 Launch Template — kubelet maxPods를 올리기 위해 필요하다.
#   io 앱(user/product)은 상시 켜진 MNG 노드에 패킹된다. 그런데 노드당 파드 수가
#   ENI 개수로 제한되면(t3.medium 17파드) 시스템 파드 6~8개를 빼고 앱은 9~11파드에서
#   막힌다 → 나머지가 Pending → 카펜터가 노드를 만든다.
#   즉 CPU는 남는데 노드만 늘어나 비용이 깎이고, 부팅 60초 동안 성능도 깎인다.
#   ★setup.sh의 ENABLE_PREFIX_DELEGATION(IP 확보)과 이 maxPods(kubelet 상한)가
#     한 쌍이다. 둘 다 있어야 파드가 실제로 늘어난다.
#   ★Bottlerocket은 user_data가 TOML이고, EKS가 자기 부트스트랩 설정과 병합한다.
#     그래서 max-pods만 지정하면 클러스터 조인은 EKS가 알아서 처리한다.
#   ★disk_size는 node_group에 둘 수 없다(launch template과 충돌) → 여기로 옮긴다.
resource "aws_launch_template" "mng" {
  name_prefix = "${local.name}-mng-"

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 20
      volume_type           = "gp3"
      delete_on_termination = true
    }
  }

  user_data = base64encode(<<-TOML
    [settings.kubernetes]
    max-pods = 110
  TOML
  )

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${local.name}-node" }
  }

  lifecycle {
    create_before_destroy = true
  }
}

# Managed Node Group
resource "aws_eks_node_group" "this" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${local.name}-ng"
  node_role_arn   = aws_iam_role.eks_node.arn
  subnet_ids      = aws_subnet.public[*].id
  instance_types  = [var.node_instance_type]
  ami_type        = "BOTTLEROCKET_x86_64"

  scaling_config {
    desired_size = var.node_desired_size
    max_size     = var.node_max_size
    min_size     = var.node_min_size
  }

  # ★disk_size는 launch_template과 동시에 쓸 수 없다 → LT의 block_device_mappings로 이동.
  launch_template {
    id      = aws_launch_template.mng.id
    version = aws_launch_template.mng.latest_version
  }

  tags = { Name = "${local.name}-node" }
}

# Allow ALB to reach pod IPs on port 8080
resource "aws_security_group_rule" "alb_to_pods" {
  type                     = "ingress"
  from_port                = 8080
  to_port                  = 8080
  protocol                 = "tcp"
  security_group_id        = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  source_security_group_id = aws_security_group.alb.id
}
