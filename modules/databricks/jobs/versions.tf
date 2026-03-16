# -----------------------------------------------------------------------------
# Provider Requirements — Jobs Module
# -----------------------------------------------------------------------------
# Declares the Databricks provider requirement.
# -----------------------------------------------------------------------------

terraform {
  required_providers {
    databricks = {
      source  = "databricks/databricks"
      version = ">= 1.50"
    }
  }
}
