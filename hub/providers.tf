# Provider Configuration — Hub Root
# Databricks workspace provider for Unity Catalog and job management.
# AWS provider (security account) for IAM role creation.

provider "databricks" {
  host  = var.databricks_workspace_url
  token = var.databricks_pat
}

# AWS provider targets the security account for IAM role management.
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
