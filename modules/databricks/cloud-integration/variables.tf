# -----------------------------------------------------------------------------
# Input Variables — Cloud Integration Module
# -----------------------------------------------------------------------------
# These variables connect Databricks storage credentials and external locations
# to the AWS IAM roles and S3 buckets created in Phases 2–4.
# -----------------------------------------------------------------------------

variable "hub_role_arn" {
  description = "ARN of the hub IAM role in the security account. Registered as a Databricks storage credential for cross-account security log access."
  type        = string
}

variable "managed_storage_role_arn" {
  description = "ARN of the managed storage IAM role. Registered as a Databricks storage credential for managed Delta table storage."
  type        = string
}

variable "managed_storage_bucket_name" {
  description = "Name of the managed storage S3 bucket in the security account (e.g., 'security-lakehouse-managed-123456')."
  type        = string
}

variable "workload_a_security_logs_bucket_name" {
  description = "Name of the security-logs S3 bucket in workload account A (e.g., 'lakehouse-workload-a-security-logs-123456')."
  type        = string
}

variable "workload_b_security_logs_bucket_name" {
  description = "Name of the security-logs S3 bucket in workload account B (e.g., 'lakehouse-workload-b-security-logs-123456')."
  type        = string
}
