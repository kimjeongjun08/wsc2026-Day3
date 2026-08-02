resource "random_id" "bucket" {
  byte_length = 4
}

# images 버킷: 실제 앱 동작용 (CloudFront 연동)
resource "aws_s3_bucket" "images" {
  bucket        = "${local.name}-images-${random_id.bucket.hex}"
  force_destroy = true
  tags          = { Name = "${local.name}-images" }
}

resource "aws_s3_bucket_public_access_block" "images" {
  bucket                  = aws_s3_bucket.images.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "images" {
  bucket = aws_s3_bucket.images.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_versioning" "images" {
  bucket = aws_s3_bucket.images.id
  versioning_configuration { status = "Disabled" }
}

resource "aws_s3_bucket_cors_configuration" "images" {
  bucket = aws_s3_bucket.images.id
  cors_rule {
    allowed_methods = ["GET", "PUT", "POST"]
    allowed_origins = ["*"]
    allowed_headers = ["*"]
    max_age_seconds = 3000
  }
}

data "aws_iam_policy_document" "images" {
  statement {
    sid     = "AllowCloudFrontReadViaOAC"
    actions = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.images.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.this.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "images" {
  bucket = aws_s3_bucket.images.id
  policy = data.aws_iam_policy_document.images.json
}

# setup 버킷: setup.sh 실행 시 사용하는 아티팩트 임시 저장용 (setup 완료 후 객체 삭제)
resource "aws_s3_bucket" "setup" {
  bucket        = "${local.name}-setup-${random_id.bucket.hex}"
  force_destroy = true
  tags          = { Name = "${local.name}-setup" }
}

resource "aws_s3_bucket_public_access_block" "setup" {
  bucket                  = aws_s3_bucket.setup.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
