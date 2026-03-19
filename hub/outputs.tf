# Outputs — Hub Root

output "hub_role_arn" {
  description = "Hub IAM role ARN."
  value       = aws_iam_role.hub.arn
}

output "managed_storage_role_arn" {
  description = "Managed storage IAM role ARN."
  value       = aws_iam_role.managed_storage.arn
}

output "hub_credential_external_id" {
  description = "Databricks-assigned external ID for the hub credential."
  value       = module.cloud_integration.hub_credential_external_id
}

output "managed_credential_external_id" {
  description = "Databricks-assigned external ID for the managed credential."
  value       = module.cloud_integration.managed_credential_external_id
}

output "catalog_name" {
  description = "Unity Catalog catalog name."
  value       = module.unity_catalog.catalog_name
}

output "cloudtrail_job_id" {
  description = "CloudTrail ingestion job ID."
  value       = module.jobs.cloudtrail_job_id
}

output "vpc_flow_job_id" {
  description = "VPC Flow Logs ingestion job ID."
  value       = module.jobs.vpc_flow_job_id
}

output "guardduty_job_id" {
  description = "GuardDuty ingestion job ID."
  value       = module.jobs.guardduty_job_id
}

output "config_job_id" {
  description = "AWS Config ingestion job ID."
  value       = module.jobs.config_job_id
}

output "threat_intel_pipeline_job_id" {
  description = "Threat intel pipeline job ID."
  value       = module.jobs.threat_intel_pipeline_job_id
}
