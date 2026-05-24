resource "aws_ecr_repository" "user" {
  name                 = "apdev-user"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

resource "aws_ecr_repository" "product" {
  name                 = "apdev-product"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

resource "aws_ecr_repository" "stress" {
  name                 = "apdev-stress"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}
