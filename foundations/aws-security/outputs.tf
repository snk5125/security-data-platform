# Outputs — Foundation Root
# Consumed by workload roots (via remote state) and hub root (via assemble script).

output "managed_storage_bucket_arn" {
  description = "Managed storage S3 bucket ARN."
  value       = module.security_foundation.managed_storage_bucket_arn
}

output "managed_storage_bucket_name" {
  description = "Managed storage S3 bucket name."
  value       = module.security_foundation.managed_storage_bucket_name
}

output "sns_topic_arn" {
  description = "SNS topic ARN for alert forwarding."
  value       = module.security_foundation.sns_topic_arn
}

output "sns_publisher_access_key_id" {
  description = "Access key ID for the SNS publisher."
  value       = module.security_foundation.sns_publisher_access_key_id
}

output "sns_publisher_secret_access_key" {
  description = "Secret access key for the SNS publisher."
  value       = module.security_foundation.sns_publisher_secret_access_key
  sensitive   = true
}

output "aws_region" {
  description = "AWS region (passed through for hub consumption)."
  value       = var.aws_region
}
