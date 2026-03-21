# Hub Root — Databricks Integration Layer
# Creates IAM roles, Databricks storage credentials, external locations,
# Unity Catalog, workspace config, and scheduled jobs.
#
# This root has both AWS and Databricks providers. The AWS provider creates
# IAM roles in the security account. The Databricks provider manages all
# workspace resources.
#
# Dependencies:
#   - Foundation root must be applied (S3 bucket, SNS topic exist)
#   - Workload roots must be applied (assemble-workloads.sh collects outputs)

# ═══════════════════════════════════════════════════════════════════════════════
# IAM Roles — see hub/iam.tf
# ═══════════════════════════════════════════════════════════════════════════════
# IAM roles are defined inline in iam.tf using deterministic ARNs to break
# the circular dependency with cloud_integration. See iam.tf for details.

# ═══════════════════════════════════════════════════════════════════════════════
# Cloud Integration (storage credentials + external locations)
# ═══════════════════════════════════════════════════════════════════════════════

module "cloud_integration" {
  source = "../modules/databricks/cloud-integration"

  # Use deterministic ARNs (from locals in iam.tf) — NOT aws_iam_role outputs.
  # This breaks the circular dependency: cloud_integration has no dependency
  # on the IAM role resources, so Terraform creates credentials first.
  hub_role_arn                = local.hub_role_arn
  managed_storage_role_arn    = local.managed_storage_role_arn
  managed_storage_bucket_name = var.managed_storage_bucket_name
  workloads                   = var.workloads
  azure_credentials           = var.azure_credentials
  gcp_credentials             = var.gcp_credentials
}

# ═══════════════════════════════════════════════════════════════════════════════
# Unity Catalog
# ═══════════════════════════════════════════════════════════════════════════════

module "unity_catalog" {
  source = "../modules/databricks/unity-catalog"

  catalog_name                = var.catalog_name
  managed_storage_bucket_name = var.managed_storage_bucket_name
  extra_schemas               = ["security"]
}

# ═══════════════════════════════════════════════════════════════════════════════
# Workspace Configuration
# ═══════════════════════════════════════════════════════════════════════════════

module "workspace_config" {
  source = "../modules/databricks/workspace-config"

  catalog_name         = var.catalog_name
  enable_cluster       = false
  enable_sql_warehouse = false
  git_repo_url         = ""
}

# ═══════════════════════════════════════════════════════════════════════════════
# Scheduled Jobs
# ═══════════════════════════════════════════════════════════════════════════════

module "jobs" {
  source = "../modules/databricks/jobs"

  catalog_name                = var.catalog_name
  managed_storage_bucket_name = var.managed_storage_bucket_name

  # Dynamic workloads map — replaces individual bucket name variables.
  workloads = { for w in var.workloads : replace(w.alias, "-", "_") => {
    cloud       = w.cloud
    storage_url = w.storage.url
  } }

  # SNS forwarding credentials from foundation root.
  sns_topic_arn                   = var.sns_topic_arn
  sns_publisher_access_key_id     = var.sns_publisher_access_key_id
  sns_publisher_secret_access_key = var.sns_publisher_secret_access_key
  aws_region                      = var.aws_region

  # Notebook paths — relative to hub/ root.
  notebook_source_dir              = "../notebooks/bronze/aws"
  silver_notebook_source_dir       = "../notebooks/silver"
  gold_notebook_source_dir         = "../notebooks/gold"
  threat_intel_notebook_source_dir = "../notebooks/security/threat_intel"
  azure_notebook_source_dir        = "../notebooks/bronze/azure"
  gcp_notebook_source_dir          = "../notebooks/bronze/gcp"

  # Workspace notebook paths.
  workspace_notebook_path              = "/Shared/security-lakehouse/bronze/aws"
  threat_intel_workspace_notebook_path = "/Shared/security-lakehouse/security/threat_intel"
  azure_workspace_notebook_path        = "/Shared/security-lakehouse/bronze/azure"
  gcp_workspace_notebook_path          = "/Shared/security-lakehouse/bronze/gcp"

  enable_scc_job = false # Set to true when SCC is activated at the org level
}
