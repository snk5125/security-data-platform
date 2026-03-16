# -----------------------------------------------------------------------------
# Input Variables — Security Account Baseline Module
# -----------------------------------------------------------------------------
# These variables parameterize the managed storage bucket and IAM roles that
# Databricks Unity Catalog needs in the security/management account.
# -----------------------------------------------------------------------------

variable "security_account_id" {
  description = "AWS account ID of the security/management account"
  type        = string
}

variable "organization_id" {
  description = "AWS Organizations ID — used to scope hub role policy conditions"
  type        = string
}

variable "managed_storage_bucket_name" {
  description = "Name for the S3 bucket where Databricks writes managed Delta tables"
  type        = string
}

variable "databricks_storage_credential_external_id" {
  description = "External ID for the managed storage role trust policy. Use '0000' on first apply; update after Phase 5."
  type        = string
  default     = "0000"
}

variable "databricks_hub_credential_external_id" {
  description = "External ID for the hub role trust policy. Use '0000' on first apply; update after Phase 5."
  type        = string
  default     = "0000"
}

# ── Databricks Unity Catalog master role ARN ──────────────────────────────────
# This is the Databricks-owned role that assumes into customer accounts for
# Unity Catalog operations. The ARN is stable across all Databricks AWS
# deployments but may differ by region. This value is for us-east-1.
variable "databricks_uc_master_role_arn" {
  description = "ARN of the Databricks Unity Catalog master role (us-east-1)"
  type        = string
  default     = "arn:aws:iam::<DATABRICKS_AWS_ACCOUNT_ID>:role/unity-catalog-prod-UCMasterRole-<SUFFIX>"
}

# ── Self-assume toggle ────────────────────────────────────────────────────────
# Databricks requires IAM roles to trust themselves (self-assume) since Jan
# 2025. However, AWS rejects a trust policy that references a role ARN that
# doesn't exist yet. On first apply (Phase 2), set this to false. On second
# apply (after Phase 5 outputs real external IDs), set to true.
variable "enable_self_assume" {
  description = "Whether to include self-assume in IAM role trust policies. Set false on first apply, true on second pass."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Additional tags to merge with module-level defaults"
  type        = map(string)
  default     = {}
}
