output "vpc_id" {
  value = aws_vpc.main.id
}

output "rds_endpoint" {
  value = aws_db_instance.mysql.endpoint
}

output "rds_proxy_endpoint" {
  value = aws_db_proxy.main.endpoint
}

output "eks_cluster_name" {
  value = aws_eks_cluster.main.name
}

output "eks_cluster_endpoint" {
  value = aws_eks_cluster.main.endpoint
}

output "ecr_user_url" {
  value = aws_ecr_repository.user.repository_url
}

output "ecr_product_url" {
  value = aws_ecr_repository.product.repository_url
}

output "ecr_stress_url" {
  value = aws_ecr_repository.stress.repository_url
}

output "s3_bucket" {
  value = aws_s3_bucket.images.bucket
}

output "ec2_instance_id" {
  value = aws_instance.app.id
}

output "cloudfront_domain" {
  value = aws_cloudfront_distribution.main.domain_name
}
