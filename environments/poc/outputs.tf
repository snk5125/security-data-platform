# -----------------------------------------------------------------------------
# Outputs — PoC Environment
# -----------------------------------------------------------------------------
# Exposes key resource identifiers for validation and downstream phases.
# -----------------------------------------------------------------------------

# ── Phase 2: Security Account Baseline ────────────────────────────────────────

output "hub_role_arn" {
  description = "Hub IAM role ARN — input to Phase 5 Databricks storage credential"
  value       = module.security_account_baseline.hub_role_arn
}

output "managed_storage_role_arn" {
  description = "Managed storage IAM role ARN — input to Phase 5 Databricks storage credential"
  value       = module.security_account_baseline.managed_storage_role_arn
}

output "managed_storage_bucket_arn" {
  description = "Managed storage S3 bucket ARN — input to Phase 5 Databricks external location"
  value       = module.security_account_baseline.managed_storage_bucket_arn
}

output "managed_storage_bucket_name" {
  description = "Managed storage S3 bucket name — used to construct s3:// URL"
  value       = module.security_account_baseline.managed_storage_bucket_name
}

# ── Phase 3: Workload Account Infrastructure ─────────────────────────────────

output "workload_a_vpc_id" {
  description = "Workload A VPC ID — consumed by Phase 4 data-sources module"
  value       = module.workload_a_baseline.vpc_id
}

output "workload_a_linux_instance_id" {
  description = "Workload A Linux EC2 instance ID"
  value       = module.workload_a_baseline.linux_instance_id
}

output "workload_a_windows_instance_id" {
  description = "Workload A Windows EC2 instance ID"
  value       = module.workload_a_baseline.windows_instance_id
}

output "workload_b_vpc_id" {
  description = "Workload B VPC ID — consumed by Phase 4 data-sources module"
  value       = module.workload_b_baseline.vpc_id
}

output "workload_b_linux_instance_id" {
  description = "Workload B Linux EC2 instance ID"
  value       = module.workload_b_baseline.linux_instance_id
}

output "workload_b_windows_instance_id" {
  description = "Workload B Windows EC2 instance ID"
  value       = module.workload_b_baseline.windows_instance_id
}

# ── Phase 4: Data Source Onboarding ──────────────────────────────────────────

output "workload_a_security_logs_bucket_arn" {
  description = "Workload A security-logs bucket ARN — input to Phase 5 Databricks external location"
  value       = module.workload_a_data_sources.security_logs_bucket_arn
}

output "workload_a_security_logs_bucket_name" {
  description = "Workload A security-logs bucket name — used for Auto Loader S3 paths"
  value       = module.workload_a_data_sources.security_logs_bucket_name
}

output "workload_a_read_only_role_arn" {
  description = "Workload A read-only IAM role ARN — assumed by hub role for Databricks access"
  value       = module.workload_a_data_sources.read_only_role_arn
}

output "workload_b_security_logs_bucket_arn" {
  description = "Workload B security-logs bucket ARN — input to Phase 5 Databricks external location"
  value       = module.workload_b_data_sources.security_logs_bucket_arn
}

output "workload_b_security_logs_bucket_name" {
  description = "Workload B security-logs bucket name — used for Auto Loader S3 paths"
  value       = module.workload_b_data_sources.security_logs_bucket_name
}

output "workload_b_read_only_role_arn" {
  description = "Workload B read-only IAM role ARN — assumed by hub role for Databricks access"
  value       = module.workload_b_data_sources.read_only_role_arn
}

# ── Phase 5: Databricks Cloud Integration ────────────────────────────────────

output "hub_credential_name" {
  description = "Hub storage credential name in Databricks — used by external locations for security log access"
  value       = module.cloud_integration.hub_credential_name
}

output "managed_credential_name" {
  description = "Managed storage credential name in Databricks — used for managed Delta table storage"
  value       = module.cloud_integration.managed_credential_name
}

output "hub_credential_external_id" {
  description = "Databricks-assigned external ID for the hub credential — feed into terraform.tfvars for Phase 5.5"
  value       = module.cloud_integration.hub_credential_external_id
}

output "managed_credential_external_id" {
  description = "Databricks-assigned external ID for the managed credential — feed into terraform.tfvars for Phase 5.5"
  value       = module.cloud_integration.managed_credential_external_id
}

# ── Phase 6: Unity Catalog ─────────────────────────────────────────────────

output "catalog_name" {
  description = "Unity Catalog catalog name — namespace for all security lakehouse tables"
  value       = module.unity_catalog.catalog_name
}

output "bronze_schema_name" {
  description = "Bronze schema name — raw ingest target for Auto Loader"
  value       = module.unity_catalog.bronze_schema_name
}

output "silver_schema_name" {
  description = "Silver schema name — normalized event target"
  value       = module.unity_catalog.silver_schema_name
}

output "gold_schema_name" {
  description = "Gold schema name — analytical products target"
  value       = module.unity_catalog.gold_schema_name
}

# ── Phase 7: Workspace Configuration ────────────────────────────────────────

output "cluster_policy_id" {
  description = "Cluster policy ID — enforces PoC cost controls"
  value       = module.workspace_config.cluster_policy_id
}

# ── Phase 8: Bronze Layer Ingestion ──────────────────────────────────────────

output "cloudtrail_job_id" {
  description = "CloudTrail bronze ingestion job ID"
  value       = module.bronze_ingestion.cloudtrail_job_id
}

output "vpc_flow_job_id" {
  description = "VPC Flow Logs bronze ingestion job ID"
  value       = module.bronze_ingestion.vpc_flow_job_id
}

output "guardduty_job_id" {
  description = "GuardDuty bronze ingestion job ID"
  value       = module.bronze_ingestion.guardduty_job_id
}

output "config_job_id" {
  description = "AWS Config bronze ingestion job ID"
  value       = module.bronze_ingestion.config_job_id
}

# ── Phase 9: SNS Alert Forwarding ────────────────────────────────────────────

output "sns_alerts_topic_arn" {
  description = "SNS topic ARN for gold.alerts forwarding — subscribe email/SQS/Lambda here"
  value       = module.sns_alerts.topic_arn
}

output "sns_alerts_topic_name" {
  description = "SNS topic name — for console navigation"
  value       = module.sns_alerts.topic_name
}

output "sns_publisher_iam_user_arn" {
  description = "IAM user ARN for the Databricks SNS publisher — for IAM audit and policy review"
  value       = module.sns_alerts.publisher_iam_user_arn
}

output "threat_intel_pipeline_job_id" {
  description = "Threat intel pipeline job ID (bronze → silver → gold → forward)"
  value       = module.bronze_ingestion.threat_intel_pipeline_job_id
}

output "alerts_secret_scope_name" {
  description = "Databricks Secret Scope name holding SNS credentials"
  value       = module.bronze_ingestion.alerts_secret_scope_name
}
