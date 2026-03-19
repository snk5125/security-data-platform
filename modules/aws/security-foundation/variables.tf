# Variables — Security Foundation Module
# Inputs for the managed storage S3 bucket and SNS alert infrastructure.

variable "security_account_id" {
  description = "AWS account ID of the security/management account."
  type        = string
}

variable "organization_id" {
  description = "AWS Organizations ID — scopes IAM conditions to the organization."
  type        = string
}

variable "managed_storage_bucket_name" {
  description = "Name for the managed storage S3 bucket (Delta tables)."
  type        = string
}

variable "databricks_uc_master_role_arn" {
  description = "Databricks Unity Catalog master role ARN (Databricks-owned account)."
  type        = string
  default     = "arn:aws:iam::<DATABRICKS_AWS_ACCOUNT_ID>:role/unity-catalog-prod-UCMasterRole-<SUFFIX>"
}

# Hub and managed-storage role names must match what hub root creates.
# These are used in the S3 bucket policy to pre-authorize the roles before
# they exist (ARN-based policies don't require principals to exist).
variable "hub_role_name" {
  description = "Name of the hub IAM role (created by the hub root). Used in bucket policy."
  type        = string
  default     = "lakehouse-hub-role"
}

variable "managed_storage_role_name" {
  description = "Name of the managed storage IAM role (created by the hub root). Used in bucket policy."
  type        = string
  default     = "lakehouse-managed-storage-role"
}

variable "sns_topic_name" {
  description = "Name for the SNS alert topic."
  type        = string
  default     = "security-lakehouse-alerts"
}

variable "sns_publisher_iam_user_name" {
  description = "Name for the IAM user that publishes to SNS."
  type        = string
  default     = "lakehouse-sns-publisher"
}

variable "tags" {
  description = "Additional tags to apply to all resources."
  type        = map(string)
  default     = {}
}
