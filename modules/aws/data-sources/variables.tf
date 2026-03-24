# -----------------------------------------------------------------------------
# Input Variables — Data Sources Module
# -----------------------------------------------------------------------------
# Configures security data sources (CloudTrail, VPC Flow Logs, GuardDuty,
# AWS Config) in a single workload account. One module invocation per account.
# -----------------------------------------------------------------------------

variable "account_alias" {
  description = "Short alias for the workload account (e.g., 'workload-a'). Used in resource naming."
  type        = string
}

variable "account_id" {
  description = "AWS account ID of the workload account. Used in IAM ARNs and bucket policy conditions."
  type        = string
}

variable "region" {
  description = "AWS region where resources are deployed. Used to construct deterministic ARNs (e.g., CloudTrail)."
  type        = string
  default     = "us-east-1"
}

variable "vpc_id" {
  description = "VPC ID from Phase 3 — VPC Flow Logs are attached to this VPC."
  type        = string
}

variable "hub_role_arn" {
  description = "ARN of the hub IAM role in the security account. The read-only role trusts this role (not Databricks directly)."
  type        = string
}

variable "tags" {
  description = "Common tags to apply to all resources."
  type        = map(string)
  default     = {}
}

variable "enable_host_telemetry" {
  description = <<-EOT
    Enable host telemetry S3 bucket and Cribl writer IAM resources. When true,
    creates a dedicated S3 bucket for Cribl Edge agent telemetry (separate from
    security logs to keep Auto Loader streams and schemas independent), plus an
    IAM user with write-only credentials for Cribl to push data.
  EOT
  type        = bool
  default     = false
}
