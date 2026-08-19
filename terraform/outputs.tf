output "endpoint" {
  description = "CloudFront endpoint"
  value       = "http://${aws_cloudfront_distribution.this.domain_name}"
}

output "alb_dns" {
  description = "ALB DNS name"
  value       = aws_lb.this.dns_name
}

output "ecr_repos" {
  value = { for k, v in aws_ecr_repository.this : k => v.repository_url }
}

# output "rds_endpoint" {
#   value = aws_db_instance.this.endpoint
# }

output "s3_bucket" {
  value = aws_s3_bucket.images.bucket
}

output "ec2_instance_id" {
  value = try(aws_instance.bastion[0].id, "")
}

output "waf_acl_arn" {
  value = aws_wafv2_web_acl.cloudfront.arn
}
