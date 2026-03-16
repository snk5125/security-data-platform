# -----------------------------------------------------------------------------
# Provider Requirements — Cloud Integration Module
# -----------------------------------------------------------------------------
# Declares the Databricks provider requirement so `terraform init` pulls the
# correct plugin version when this module is used standalone or composed.
# -----------------------------------------------------------------------------

terraform {
  required_providers {
    databricks = {
      source  = "databricks/databricks"
      version = ">= 1.50"
    }
  }
}
