# Provider Configuration — AWS Workload Root
# Single AWS provider targeting this workload account via
# OrganizationAccountAccessRole.

provider "aws" {
  region = var.aws_region

  assume_role {
    role_arn = "arn:aws:iam::${var.account_id}:role/OrganizationAccountAccessRole"
  }

  default_tags {
    tags = {
      Project     = "security-lakehouse"
      Environment = "poc"
      ManagedBy   = "terraform"
    }
  }
}
