variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "project" {
  type    = string
  default = "wsi2026"
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
  default = "1.33"
}

variable "node_instance_type" {
  type        = string
  description = "EKS 노드 인스턴스 타입 (기본값: t3.medium)"
}

variable "node_desired_size" {
  type    = number
  default = 2
}

variable "node_max_size" {
  type    = number
  default = 4
}

variable "node_min_size" {
  type    = number
  default = 2
}

variable "db_name" {
  type    = string
  default = "dev"
}

variable "db_instance_class" {
  type        = string
  description = "RDS 인스턴스 클래스 (기본값: db.t3.micro)"
}

variable "app_image_tag" {
  type        = string
  default     = "latest"
  description = "Tag of the user/product/stress images pushed to ECR"
}
