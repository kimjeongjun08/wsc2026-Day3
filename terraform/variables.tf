variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "project" {
  type    = string
  default = "apdev"
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "azs" {
  type    = list(string)
  default = ["ap-northeast-2a", "ap-northeast-2b"]
}

variable "eks_version" {
  type    = string
  default = "1.36"
}

variable "node_instance_type" {
  type        = string
  description = "EKS 노드 인스턴스 타입 (기본값: t3.medium)"
}

variable "node_desired_size" {
  type    = number
  default = 1 # MNG 1대 = user+product 패킹. stress는 anti-affinity로 카펜터 전용노드 1대 → 총 baseline 2대.
}

variable "node_max_size" {
  type    = number
  default = 1 # MNG 1 고정(탄력 스케일은 Karpenter). MNG 2로 하면 user/product가 두 노드에 퍼져 stress가 3번째로 밀림.
}

variable "node_min_size" {
  type    = number
  default = 1 # MNG 1 고정.
}

variable "db_name" {
  type    = string
  default = "dev"
}

variable "db_instance_class" {
  type        = string
  description = "RDS 인스턴스 클래스 (기본값: db.t3.micro)"
}

variable "db_allocated_storage" {
  type        = number
  description = "RDS 스토리지 GB (연습: 200, 대회: 500+면 IOPS/처리량 조작 가능, 최대: 65536)"
}

variable "app_image_tag" {
  type        = string
  default     = "latest"
  description = "Tag of the user/product/stress images pushed to ECR"
}

variable "bastion_enabled" {
  description = "설치용 bastion 생성 여부. 설치가 끝나면 false 로 다시 apply 해서 없앤다(비용 지표·스펙 준수)."
  type        = bool
  default     = true
}
