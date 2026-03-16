# -----------------------------------------------------------------------------
# Input Variables — PoC Environment
# -----------------------------------------------------------------------------
# These variables parameterize the multi-account deployment. Values are
# supplied via terraform.tfvars. Sensitive values (PAT, external IDs) should
# be set via environment variables or a .tfvars file excluded from version
# control.
# -----------------------------------------------------------------------------

# ── AWS Account Topology ──────────────────────────────────────────────────────

variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "security_account_id" {
  description = "AWS account ID of the security/management account (hosts state, hub role, managed storage)"
  type        = string
}

variable "workload_a_account_id" {
  description = "AWS account ID of workload account A (hosts VPC, EC2, security data sources)"
  type        = string
}

variable "workload_b_account_id" {
  description = "AWS account ID of workload account B (hosts VPC, EC2, security data sources)"
  type        = string
}

variable "organization_id" {
  description = "AWS Organizations ID — used to scope IAM conditions to the organization"
  type        = string
}

# ── Databricks ────────────────────────────────────────────────────────────────

variable "databricks_workspace_url" {
  description = "Databricks workspace URL (e.g., https://dbc-xxxxx.cloud.databricks.com). Used starting in Phase 5."
  type        = string
  default     = ""
}

variable "databricks_pat" {
  description = "Databricks personal access token for workspace-level provider authentication. Used starting in Phase 5."
  type        = string
  sensitive   = true
  default     = ""
}

# ── External IDs (two-pass pattern) ──────────────────────────────────────────
# These start as "0000" on first apply. After Phase 5 creates the Databricks
# storage credentials, the real external IDs are output and must be fed back
# into terraform.tfvars for a second apply to update the IAM trust policies.

variable "databricks_storage_credential_external_id" {
  description = "External ID for the managed storage IAM role trust policy. Set to '0000' until Phase 5 outputs the real value."
  type        = string
  default     = "0000"
}

variable "databricks_hub_credential_external_id" {
  description = "External ID for the hub IAM role trust policy. Set to '0000' until Phase 5 outputs the real value."
  type        = string
  default     = "0000"
}

variable "enable_self_assume" {
  description = "Whether IAM roles include self-assume in trust policies (required by Databricks since Jan 2025). Set true after Phase 5."
  type        = bool
  default     = false
}
