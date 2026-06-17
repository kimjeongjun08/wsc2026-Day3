locals {
  name       = var.project
  account_id = data.aws_caller_identity.current.account_id
  tags = {
    Project = var.project
  }
}
