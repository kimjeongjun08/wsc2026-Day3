resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${local.name}-vpc" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${local.name}-igw" }
}

# Single tier of public subnets — hosts ALB, EKS nodes, RDS.
# No NAT, no private/db subnets. Single route table.
resource "aws_subnet" "public" {
  count                   = length(var.azs)
  vpc_id                  = aws_vpc.this.id
  # ★/20 (4096개, /28 블록 256개). 8비트로 쪼개면 /24 라 /28 블록이 16개뿐이다.
  #   prefix delegation 은 "연속된 /28 블록" 을 통째로 잡는다 — 총 여유 IP 가 남아 있어도
  #   블록이 조각나면 신규 노드가 prefix 를 못 받아 파드가 ContainerCreating 에 갇힌다.
  #   실측: /24 + maxPods 110 으로 노드 6대까지 가자 user 파드 20개 중 9개가 IP 를 못 받았다.
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, count.index)
  availability_zone       = var.azs[count.index]
  map_public_ip_on_launch = true
  tags = {
    Name                                          = "${local.name}-public-${var.azs[count.index]}"
    "kubernetes.io/role/elb"                      = "1"
    "kubernetes.io/role/internal-elb"             = "1"
    "kubernetes.io/cluster/${local.name}-cluster" = "shared"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${local.name}-rt-public" }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Free S3 gateway endpoint — saves on inter-AZ data transfer for image uploads
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]
  tags              = { Name = "${local.name}-vpce-s3" }
}
