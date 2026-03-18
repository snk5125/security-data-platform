# -----------------------------------------------------------------------------
# Outputs — SNS Alerts Module
# -----------------------------------------------------------------------------
# Consumed by the Databricks jobs module to configure the Databricks Secret
# Scope used by the alert forwarding notebook.
# -----------------------------------------------------------------------------

output "topic_arn" {
  description = "SNS topic ARN — used as the publish destination in the Databricks forwarding notebook"
  value       = aws_sns_topic.alerts.arn
}

output "topic_name" {
  description = "SNS topic name — useful for console navigation and subscription management"
  value       = aws_sns_topic.alerts.name
}

output "publisher_access_key_id" {
  description = "IAM access key ID for the SNS publisher user — stored in Databricks Secrets"
  value       = aws_iam_access_key.sns_publisher.id
}

output "publisher_secret_access_key" {
  description = "IAM access key secret for the SNS publisher user — stored in Databricks Secrets (sensitive)"
  value       = aws_iam_access_key.sns_publisher.secret
  sensitive   = true
}

output "publisher_iam_user_arn" {
  description = "ARN of the SNS publisher IAM user — referenced in the SNS topic policy"
  value       = aws_iam_user.sns_publisher.arn
}

output "aws_region" {
  description = "AWS region where the SNS topic was created — needed by the boto3 client in the notebook"
  value       = data.aws_region.current.name
}
