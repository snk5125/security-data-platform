# -----------------------------------------------------------------------------
# Outputs — consumed by environments/poc/backend.tf
# -----------------------------------------------------------------------------
# After applying the bootstrap configuration, copy these values into the
# backend block of each environment. They are also printed to the console
# for convenience.
# -----------------------------------------------------------------------------

output "state_bucket_name" {
  description = "S3 bucket name for Terraform remote state"
  value       = aws_s3_bucket.terraform_state.id
}

output "lock_table_name" {
  description = "DynamoDB table name for Terraform state locking"
  value       = aws_dynamodb_table.terraform_locks.name
}

output "state_bucket_arn" {
  description = "S3 bucket ARN (useful for IAM policies restricting state access)"
  value       = aws_s3_bucket.terraform_state.arn
}
