# -----------------------------------------------------------------------------
# Outputs — Ingestion Jobs Module (Bronze, Silver, Gold)
# -----------------------------------------------------------------------------
# Exposes job and notebook identifiers for validation and downstream use.
# -----------------------------------------------------------------------------

output "cloudtrail_job_id" {
  description = "Job ID for the CloudTrail bronze ingestion job"
  value       = databricks_job.cloudtrail.id
}

output "vpc_flow_job_id" {
  description = "Job ID for the VPC Flow Logs bronze ingestion job"
  value       = databricks_job.vpc_flow.id
}

output "guardduty_job_id" {
  description = "Job ID for the GuardDuty bronze ingestion job"
  value       = databricks_job.guardduty.id
}

output "config_job_id" {
  description = "Job ID for the AWS Config bronze ingestion job"
  value       = databricks_job.config.id
}

output "threat_intel_pipeline_job_id" {
  description = "Job ID for the threat intel pipeline (bronze → silver → gold → forward)"
  value       = databricks_job.threat_intel_pipeline.id
}

output "alerts_secret_scope_name" {
  description = "Databricks Secret Scope name holding SNS credentials — 'security-lakehouse'"
  value       = databricks_secret_scope.security_lakehouse.name
}

output "notebook_paths" {
  description = "Map of data source to notebook path in the workspace"
  value = {
    ocsf_common         = databricks_notebook.ocsf_common.path
    cloudtrail          = databricks_notebook.cloudtrail.path
    vpc_flow            = databricks_notebook.vpc_flow.path
    guardduty           = databricks_notebook.guardduty.path
    config              = databricks_notebook.config.path
    config_cdc          = databricks_notebook.config_cdc.path
    ec2_inventory       = databricks_notebook.ec2_inventory.path
    gold_alerts         = databricks_notebook.gold_alerts.path
    gold_alerts_forward = databricks_notebook.gold_alerts_forward.path
  }
}
