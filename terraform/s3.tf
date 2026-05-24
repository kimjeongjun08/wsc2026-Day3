data "aws_caller_identity" "current" {}

# Product images bucket
resource "aws_s3_bucket" "images" {
  bucket        = "apdev-product-images-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
  tags          = { Name = "apdev-product-images" }
}

# Artifacts bucket for app binaries
resource "aws_s3_bucket" "artifacts" {
  bucket        = "apdev-artifacts-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
  tags          = { Name = "apdev-artifacts" }
}

# Upload application binaries
resource "aws_s3_object" "user_bin" {
  bucket = aws_s3_bucket.artifacts.id
  key    = "apps/user"
  source = "${path.module}/application/user/user"
  etag   = filemd5("${path.module}/application/user/user")
}

resource "aws_s3_object" "product_bin" {
  bucket = aws_s3_bucket.artifacts.id
  key    = "apps/product"
  source = "${path.module}/application/product/product"
  etag   = filemd5("${path.module}/application/product/product")
}

resource "aws_s3_object" "stress_bin" {
  bucket = aws_s3_bucket.artifacts.id
  key    = "apps/stress"
  source = "${path.module}/application/stress/stress"
  etag   = filemd5("${path.module}/application/stress/stress")
}
