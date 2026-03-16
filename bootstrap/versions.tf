# -----------------------------------------------------------------------------
# Provider and Terraform version constraints for the bootstrap configuration.
# This is intentionally minimal — only the AWS provider is needed to create
# the S3 bucket and DynamoDB table that store Terraform state for all
# subsequent phases.
# -----------------------------------------------------------------------------
terraform {
  required_version = ">= 1.5, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
  }
}

provider "aws" {
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = "security-lakehouse"
      Environment = "poc"
      ManagedBy   = "terraform"
      Component   = "bootstrap"
    }
  }
}
