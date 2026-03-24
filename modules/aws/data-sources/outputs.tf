# -----------------------------------------------------------------------------
# Outputs — Data Sources Module
# -----------------------------------------------------------------------------
# Exposes key resource identifiers consumed by downstream phases:
#   - Phase 5: security_logs_bucket_arn → Databricks external locations
#   - Phase 8: security_logs_bucket_name → Auto Loader S3 paths
#   - Phase 2 update: read_only_role_arn → hub role policy tightening
# -----------------------------------------------------------------------------

output "security_logs_bucket_arn" {
  description = "ARN of the security-logs S3 bucket — used by Phase 5 for Databricks external locations"
  value       = aws_s3_bucket.security_logs.arn
}

output "security_logs_bucket_name" {
  description = "Name of the security-logs S3 bucket — used to construct s3:// URLs for Auto Loader"
  value       = aws_s3_bucket.security_logs.id
}

output "read_only_role_arn" {
  description = "ARN of the read-only IAM role — the hub role chain-assumes this to access security logs"
  value       = aws_iam_role.read_only.arn
}

output "guardduty_detector_id" {
  description = "GuardDuty detector ID — useful for validation and future configuration"
  value       = aws_guardduty_detector.main.id
}

output "kms_key_arn" {
  description = "KMS key ARN used for GuardDuty S3 export encryption"
  value       = aws_kms_key.guardduty.arn
}

output "cloudtrail_arn" {
  description = "CloudTrail trail ARN — useful for validation"
  value       = aws_cloudtrail.main.arn
}

# --- Host Telemetry Outputs (conditional) ---

output "host_telemetry_storage_url" {
  description = "S3 URL of the host telemetry bucket — used for Databricks external locations and Auto Loader. Empty string when host telemetry is disabled."
  value       = var.enable_host_telemetry ? "s3://${aws_s3_bucket.host_telemetry[0].id}/" : ""
}

output "host_telemetry_write_credentials" {
  description = "Cribl writer IAM access key credentials for the host telemetry bucket. Null when host telemetry is disabled."
  sensitive   = true
  value = var.enable_host_telemetry ? {
    access_key_id     = aws_iam_access_key.cribl_writer[0].id
    secret_access_key = aws_iam_access_key.cribl_writer[0].secret
  } : null
}
