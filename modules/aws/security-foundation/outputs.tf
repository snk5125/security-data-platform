# Outputs — Security Foundation Module

output "managed_storage_bucket_arn" {
  description = "Managed storage S3 bucket ARN."
  value       = aws_s3_bucket.managed_storage.arn
}

output "managed_storage_bucket_name" {
  description = "Managed storage S3 bucket name."
  value       = aws_s3_bucket.managed_storage.id
}

output "sns_topic_arn" {
  description = "SNS topic ARN for alert forwarding."
  value       = aws_sns_topic.alerts.arn
}

output "sns_topic_name" {
  description = "SNS topic name."
  value       = aws_sns_topic.alerts.name
}

output "sns_publisher_access_key_id" {
  description = "Access key ID for the SNS publisher IAM user."
  value       = aws_iam_access_key.sns_publisher.id
}

output "sns_publisher_secret_access_key" {
  description = "Secret access key for the SNS publisher IAM user."
  value       = aws_iam_access_key.sns_publisher.secret
  sensitive   = true
}

output "sns_publisher_iam_user_arn" {
  description = "IAM user ARN for the SNS publisher."
  value       = aws_iam_user.sns_publisher.arn
}
