# -----------------------------------------------------------------------------
# PoC Environment — Root Module
# -----------------------------------------------------------------------------
# Wires together all modules for the multi-account security lakehouse.
# Resources are added phase-by-phase as the implementation progresses.
#
# Phase 2: Security account baseline (managed storage bucket + IAM roles)
# Phase 3: Workload account infrastructure (VPC, EC2 in each workload account)
# Phase 4: Data source onboarding (CloudTrail, Flow Logs, GuardDuty, Config)
# Phase 5: Databricks cloud integration (storage credentials, external locations)
# Phase 6: Unity Catalog (catalog, schemas, grants)
# Phase 7: Workspace configuration (cluster policy, cluster, optional SQL/git)
# -----------------------------------------------------------------------------

# ═════════════════════════════════════════════════════════════════════════════
# Phase 2: Security Account Foundation
# ═════════════════════════════════════════════════════════════════════════════
# Creates the managed storage bucket and IAM roles in the security account.
# The default AWS provider targets this account, so no provider alias needed.

module "security_account_baseline" {
  source = "../../modules/aws/security-account-baseline"

  security_account_id         = var.security_account_id
  organization_id             = var.organization_id
  managed_storage_bucket_name = "security-lakehouse-managed-${var.security_account_id}"

  # External IDs start as "0000" — updated after Phase 5 creates Databricks
  # storage credentials and outputs the real external IDs.
  databricks_storage_credential_external_id = var.databricks_storage_credential_external_id
  databricks_hub_credential_external_id     = var.databricks_hub_credential_external_id

  # Self-assume is required by Databricks since Jan 2025. Set to true after
  # Phase 5 creates the storage credentials (the roles must exist first).
  enable_self_assume = var.enable_self_assume

  tags = local.common_tags
}

# ═════════════════════════════════════════════════════════════════════════════
# Phase 3: Workload Account Infrastructure
# ═════════════════════════════════════════════════════════════════════════════
# Deploys VPC, networking, and EC2 instances into each workload account.
# These instances generate security events (CloudTrail, Flow Logs, GuardDuty,
# Config) that feed the lakehouse in Phase 4.
#
# One module block per account because Terraform does not support for_each
# on provider aliases. Each block passes the appropriate aliased provider.

module "workload_a_baseline" {
  source = "../../modules/aws/workload-account-baseline"

  providers = {
    aws = aws.workload_a
  }

  account_alias      = "workload-a"
  account_id         = var.workload_a_account_id
  vpc_cidr           = "10.0.0.0/16"
  public_subnet_cidr = "10.0.1.0/24"

  tags = local.common_tags
}

module "workload_b_baseline" {
  source = "../../modules/aws/workload-account-baseline"

  providers = {
    aws = aws.workload_b
  }

  account_alias      = "workload-b"
  account_id         = var.workload_b_account_id
  vpc_cidr           = "10.1.0.0/16"
  public_subnet_cidr = "10.1.1.0/24"

  tags = local.common_tags
}

# ═════════════════════════════════════════════════════════════════════════════
# Phase 4: Data Source Onboarding
# ═════════════════════════════════════════════════════════════════════════════
# Enables security data sources (CloudTrail, VPC Flow Logs, GuardDuty, AWS
# Config) in each workload account. Each module invocation creates:
#   - S3 bucket for security logs (prefix-separated by data source)
#   - KMS key for GuardDuty export encryption
#   - Read-only IAM role trusted by the hub role (for Databricks access chain)
#   - CloudTrail trail, VPC Flow Log, GuardDuty detector + S3 export,
#     Config recorder + delivery channel
#
# Dependencies: Phase 3 VPC IDs (for Flow Logs) and Phase 2 hub role ARN
# (for the read-only role trust policy).

module "workload_a_data_sources" {
  source = "../../modules/aws/data-sources"

  providers = {
    aws = aws.workload_a
  }

  account_alias = "workload-a"
  account_id    = var.workload_a_account_id
  region        = var.aws_region
  vpc_id        = module.workload_a_baseline.vpc_id
  hub_role_arn  = module.security_account_baseline.hub_role_arn

  tags = local.common_tags
}

module "workload_b_data_sources" {
  source = "../../modules/aws/data-sources"

  providers = {
    aws = aws.workload_b
  }

  account_alias = "workload-b"
  account_id    = var.workload_b_account_id
  region        = var.aws_region
  vpc_id        = module.workload_b_baseline.vpc_id
  hub_role_arn  = module.security_account_baseline.hub_role_arn

  tags = local.common_tags
}

# ═════════════════════════════════════════════════════════════════════════════
# Phase 5: Databricks Cloud Integration
# ═════════════════════════════════════════════════════════════════════════════
# Connects Databricks Unity Catalog to AWS by registering IAM roles as storage
# credentials and creating external locations for each S3 bucket. This is the
# bridge between the AWS infrastructure (Phases 2–4) and Databricks (Phase 6+).
#
# The Databricks provider uses workspace-level PAT authentication configured
# in providers.tf. No provider alias needed — there is only one workspace.
#
# Dependencies:
#   - Phase 2: hub_role_arn, managed_storage_role_arn, managed_storage_bucket_name
#   - Phase 4: workload_a/b_security_logs_bucket_name
#
# After apply: output external IDs → update terraform.tfvars → Phase 5.5 re-apply

module "cloud_integration" {
  source = "../../modules/databricks/cloud-integration"

  hub_role_arn                = module.security_account_baseline.hub_role_arn
  managed_storage_role_arn    = module.security_account_baseline.managed_storage_role_arn
  managed_storage_bucket_name = module.security_account_baseline.managed_storage_bucket_name

  workload_a_security_logs_bucket_name = module.workload_a_data_sources.security_logs_bucket_name
  workload_b_security_logs_bucket_name = module.workload_b_data_sources.security_logs_bucket_name
}

# ═════════════════════════════════════════════════════════════════════════════
# Phase 6: Unity Catalog
# ═════════════════════════════════════════════════════════════════════════════
# Creates the catalog, medallion schemas (bronze/silver/gold), and grants.
# This is the data governance layer that organizes all security telemetry
# tables within Databricks Unity Catalog.
#
# The free trial workspace has an auto-provisioned metastore — no explicit
# metastore creation is needed. The catalog inherits its managed storage
# location from the metastore.
#
# Dependencies:
#   - Phase 5: storage credentials and external locations must exist
#   - Phase 5.5: IAM trust policies updated with real external IDs

module "unity_catalog" {
  source = "../../modules/databricks/unity-catalog"

  catalog_name                = "security_poc"
  managed_storage_bucket_name = module.security_account_baseline.managed_storage_bucket_name
}

# ═════════════════════════════════════════════════════════════════════════════
# Phase 7: Workspace Configuration
# ═════════════════════════════════════════════════════════════════════════════
# Configures workspace-level resources for the PoC. The free trial workspace
# is Free Edition — it has no classic compute plane (no worker environments),
# so classic clusters cannot be created. A serverless "Starter Warehouse"
# is auto-provisioned by Databricks and available for ad-hoc queries.
#
# This module creates a cluster policy (for future use if upgraded to a paid
# tier) and skips cluster/warehouse creation since the workspace already
# provides serverless compute.
#
# Dependencies:
#   - Phase 6: catalog must exist

module "workspace_config" {
  source = "../../modules/databricks/workspace-config"

  catalog_name         = "security_poc"
  enable_cluster       = false
  enable_sql_warehouse = false
  git_repo_url         = ""
}

# ═════════════════════════════════════════════════════════════════════════════
# Phase 9: SNS Alert Forwarding Infrastructure
# ═════════════════════════════════════════════════════════════════════════════
# Creates the AWS-side resources needed to receive forwarded gold.alerts rows
# from Databricks: an SNS topic, a least-privilege IAM publisher user, and
# the access key that Databricks uses to authenticate sns:Publish calls.
#
# The access key credentials are passed directly into Phase 8's Databricks
# jobs module, which stores them in the "security-lakehouse" Databricks Secret
# Scope so the forwarding notebook can retrieve them at runtime.
#
# Dependencies:
#   - Phase 2: security account provider configured (SNS + IAM in same account)
#   - Phase 8 (implicitly): the jobs module consumes the outputs of this module

module "sns_alerts" {
  source = "../../modules/aws/sns-alerts"

  # Default AWS provider targets the security account — SNS topic and IAM
  # user live alongside the managed storage bucket and hub role.

  tags = local.common_tags
}

# ═════════════════════════════════════════════════════════════════════════════
# Phase 8: Bronze Layer Ingestion
# ═════════════════════════════════════════════════════════════════════════════
# Uploads Auto Loader notebooks and creates scheduled jobs for bronze layer
# ingestion. Each job reads from both workload account S3 buckets and writes
# raw data into the bronze schema (security_poc.bronze.*).
#
# Jobs use serverless compute and are created PAUSED — unpause after
# validating that the notebooks run correctly.
#
# Dependencies:
#   - Phase 6: catalog and bronze schema must exist
#   - Phase 7: serverless compute available
#   - Phase 4: S3 data flowing (at least 30 minutes of data)

module "bronze_ingestion" {
  source = "../../modules/databricks/jobs"

  catalog_name                         = "security_poc"
  managed_storage_bucket_name          = module.security_account_baseline.managed_storage_bucket_name
  workload_a_security_logs_bucket_name = module.workload_a_data_sources.security_logs_bucket_name
  workload_b_security_logs_bucket_name = module.workload_b_data_sources.security_logs_bucket_name

  # SNS forwarding credentials — sourced from the sns_alerts module output.
  # These are stored in the Databricks "security-lakehouse" Secret Scope by the
  # jobs module so the forwarding notebook can retrieve them at runtime.
  sns_topic_arn                   = module.sns_alerts.topic_arn
  sns_publisher_access_key_id     = module.sns_alerts.publisher_access_key_id
  sns_publisher_secret_access_key = module.sns_alerts.publisher_secret_access_key
  aws_region                      = var.aws_region
}
