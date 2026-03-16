# -----------------------------------------------------------------------------
# Outputs — Security Account Baseline Module
# -----------------------------------------------------------------------------
# These outputs are consumed downstream by Phase 5 (Databricks Unity Catalog)
# to create storage credentials and external locations.
# -----------------------------------------------------------------------------

output "hub_role_arn" {
  description = "ARN of the hub IAM role — used as the Databricks storage credential for cross-account access"
  value       = aws_iam_role.hub.arn
}

output "hub_role_name" {
  description = "Name of the hub IAM role — useful for validation commands"
  value       = aws_iam_role.hub.name
}

output "managed_storage_role_arn" {
  description = "ARN of the managed storage IAM role — used as the Databricks storage credential for managed tables"
  value       = aws_iam_role.managed_storage.arn
}

output "managed_storage_role_name" {
  description = "Name of the managed storage IAM role — useful for validation commands"
  value       = aws_iam_role.managed_storage.name
}

output "managed_storage_bucket_arn" {
  description = "ARN of the managed storage S3 bucket — used for Databricks external location"
  value       = aws_s3_bucket.managed_storage.arn
}

output "managed_storage_bucket_name" {
  description = "Name of the managed storage S3 bucket — used to construct the s3:// URL for Databricks"
  value       = aws_s3_bucket.managed_storage.id
}
