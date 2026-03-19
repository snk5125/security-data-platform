# Provider Configuration — Foundation Root
# Single AWS provider targeting the security/management account.
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "security-lakehouse"
      Environment = "poc"
      ManagedBy   = "terraform"
    }
  }
}
