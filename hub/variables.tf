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
variable "workloads" {
  description = "List of workload manifests from assemble-workloads.sh."
  type = list(object({
    cloud      = string
    account_id = string
    alias      = string
    region     = string
    storage = object({
      type        = string
      bucket_name = string
      bucket_arn  = string
    })
    read_only_role_arn = string
    encryption = object({
      type    = string
      key_arn = string
    })
    data_products = map(object({
      format      = string
      path_prefix = string
    }))
  }))
  default = []
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
