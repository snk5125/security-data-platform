# -----------------------------------------------------------------------------
# Provider Requirements — Workload Account Baseline Module
# -----------------------------------------------------------------------------
# Declares the providers this module expects. The root module passes the
# appropriate provider alias (e.g., aws.workload_a) via the providers block.
# -----------------------------------------------------------------------------

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.50"
    }
    tls = {
      source  = "hashicorp/tls"
      version = ">= 4.0"
    }
  }
}
