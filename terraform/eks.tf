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

  disk_size = 20

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
