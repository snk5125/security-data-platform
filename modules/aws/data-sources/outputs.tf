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
