variable "region" {
  default = "ap-northeast-2"
}

variable "cluster_name" {
  default = "apdev-eks-cluster"
}

variable "db_username" {
  default = "admin"
}

variable "db_password" {
  description = "RDS admin password"
  sensitive   = true
  type        = string
}

variable "db_name" {
  default = "dev"
}

variable "node_instance_type" {
  description = "EKS node instance type (대회 당일 변경)"
  default     = "t3.medium"
}
