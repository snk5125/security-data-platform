# -----------------------------------------------------------------------------
# Terraform and provider version constraints for the PoC environment.
# -----------------------------------------------------------------------------
terraform {
  required_version = ">= 1.5, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.50"
    }
    # TLS provider generates SSH key pairs for EC2 instances (Phase 3).
    # Keys are stored in Terraform state only — not written to disk.
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}
