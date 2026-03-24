# Variables — Hub Root

variable "aws_region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "databricks_workspace_url" {
  description = "Databricks workspace URL."
  type        = string
}

variable "databricks_pat" {
  description = "Databricks personal access token."
  type        = string
  sensitive   = true
}

variable "security_account_id" {
  description = "AWS account ID of the security/management account."
  type        = string
}

variable "organization_id" {
  description = "AWS Organizations ID."
  type        = string
}

# Foundation outputs — passed via terraform.tfvars or remote state.
variable "managed_storage_bucket_name" {
  description = "Managed storage S3 bucket name (from foundation root)."
  type        = string
}

variable "managed_storage_bucket_arn" {
  description = "Managed storage S3 bucket ARN (from foundation root)."
  type        = string
}

variable "sns_topic_arn" {
  description = "SNS topic ARN for alert forwarding (from foundation root)."
  type        = string
}

variable "sns_publisher_access_key_id" {
  description = "SNS publisher access key ID (from foundation root)."
  type        = string
}

variable "sns_publisher_secret_access_key" {
  description = "SNS publisher secret access key (from foundation root)."
  type        = string
  sensitive   = true
}

# Workload manifests — populated by assemble-workloads.sh.
# AWS workloads populate all fields. Azure workloads omit read_only_role_arn,
# bucket_name, bucket_arn, and encryption — they default to empty/none values.
variable "workloads" {
  description = "List of workload manifests from assemble-workloads.sh. Supports AWS and Azure workloads."
  type = list(object({
    cloud      = string # "aws", "azure", or "gcp"
    account_id = string # AWS account ID or Azure subscription ID
    alias      = string
    region     = string
    storage = object({
      type        = string               # "s3", "adls", or "gcs"
      url         = optional(string, "") # "s3://bucket/" or "abfss://container@account.dfs.core.windows.net/"
      bucket_name = optional(string, "")
      bucket_arn  = optional(string, "")
    })
    read_only_role_arn = optional(string, "")
    encryption = optional(object({
      type    = string
      key_arn = string
    }), { type = "none", key_arn = "" })
    data_products = map(object({
      format      = string
      path_prefix = string
    }))
    host_telemetry = optional(object({
      storage_url = optional(string, "")
    }), { storage_url = "" })
  }))
  default = []
}

variable "azure_credentials" {
  description = "Azure service principal credentials for ADLS access via Databricks. Null if no Azure workloads exist."
  type = object({
    directory_id   = string
    application_id = string
    client_secret  = string
  })
  default   = null
  sensitive = true
}

variable "gcp_credentials" {
  description = "GCP service account key credentials for GCS access via Databricks. Null if no GCP workloads exist."
  type = object({
    client_email   = string
    private_key_id = string
    private_key    = string
  })
  default   = null
  sensitive = true
}

variable "catalog_name" {
  description = "Unity Catalog catalog name."
  type        = string
  default     = "security_poc"
}

variable "databricks_uc_master_role_arn" {
  description = "Databricks Unity Catalog master role ARN (Databricks-owned account)."
  type        = string
  default     = "arn:aws:iam::<DATABRICKS_AWS_ACCOUNT_ID>:role/unity-catalog-prod-UCMasterRole-<SUFFIX>"
}
