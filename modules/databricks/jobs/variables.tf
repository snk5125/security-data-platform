# -----------------------------------------------------------------------------
# Input Variables — Ingestion Jobs Module (Bronze, Silver, Gold)
# -----------------------------------------------------------------------------
# Parameterizes the notebook paths, S3 bucket names, and checkpoint location
# for the ingestion jobs across all medallion tiers.
#
# Prerequisites:
#   - Phase 6 complete (catalog and schemas exist)
#   - Phase 7 complete (compute available — serverless or cluster)
#   - Data flowing to S3 (Phase 4 applied at least 30 minutes ago)
# -----------------------------------------------------------------------------

variable "catalog_name" {
  description = "Unity Catalog catalog name — used as the target catalog for bronze tables"
  type        = string
  default     = "security_poc"
}

variable "managed_storage_bucket_name" {
  description = "Managed storage S3 bucket name — used for Auto Loader checkpoint storage"
  type        = string
}

variable "workloads" {
  description = "Map of workload alias to cloud type and storage URL. Used to generate job parameters dynamically."
  type = map(object({
    cloud                      = string
    storage_url                = string
    host_telemetry_storage_url = optional(string, "")
  }))
  default = {}
}

variable "notebook_source_dir" {
  description = "Local path to the bronze notebook source files (relative to the root module)"
  type        = string
  default     = "../../notebooks/bronze/aws"
}

variable "workspace_notebook_path" {
  description = "Workspace path prefix where bronze notebooks are uploaded"
  type        = string
  default     = "/Shared/security-lakehouse/bronze/aws"
}

variable "silver_notebook_source_dir" {
  description = "Local path to the silver notebook source files (relative to the root module)"
  type        = string
  default     = "../../notebooks/silver"
}

variable "silver_workspace_notebook_path" {
  description = "Workspace path prefix where silver notebooks are uploaded"
  type        = string
  default     = "/Shared/security-lakehouse/silver"
}

variable "gold_notebook_source_dir" {
  description = "Local path to the gold notebook source files (relative to the root module)"
  type        = string
  default     = "../../notebooks/gold"
}

variable "gold_workspace_notebook_path" {
  description = "Workspace path prefix where gold notebooks are uploaded"
  type        = string
  default     = "/Shared/security-lakehouse/gold"
}

variable "threat_intel_notebook_source_dir" {
  description = "Local path to the threat intel notebook source files (relative to the root module)"
  type        = string
  default     = "../../notebooks/security/threat_intel"
}

variable "threat_intel_workspace_notebook_path" {
  description = "Workspace path prefix where threat intel notebooks are uploaded"
  type        = string
  default     = "/Shared/security-lakehouse/security/threat_intel"
}

variable "azure_notebook_source_dir" {
  description = "Local path to the Azure bronze notebook source files"
  type        = string
  default     = "../../notebooks/bronze/azure"
}

variable "azure_workspace_notebook_path" {
  description = "Workspace path prefix where Azure bronze notebooks are uploaded"
  type        = string
  default     = "/Shared/security-lakehouse/bronze/azure"
}

variable "gcp_notebook_source_dir" {
  description = "Local path to the GCP bronze notebook source files"
  type        = string
  default     = "../../notebooks/bronze/gcp"
}

variable "gcp_workspace_notebook_path" {
  description = "Workspace path prefix where GCP bronze notebooks are uploaded"
  type        = string
  default     = "/Shared/security-lakehouse/bronze/gcp"
}

variable "enable_scc_job" {
  description = "Enable the GCP SCC Findings job. Requires SCC org-level activation. Default: false."
  type        = bool
  default     = false
}

variable "host_telemetry_notebook_source_path" {
  description = "Local path to host telemetry notebook directory. Empty string disables host telemetry job creation."
  type        = string
  default     = ""
}

variable "host_telemetry_notebook_workspace_path" {
  description = "Databricks workspace path for host telemetry notebooks"
  type        = string
  default     = "/Shared/security-lakehouse/bronze/host_telemetry"
}

# ─────────────────────────────────────────────────────────────────────────────
# SNS Alert Forwarding
# These variables are passed from the sns-alerts AWS module outputs via the
# root module. The jobs module stores them in a Databricks Secret Scope so the
# forwarding notebook can retrieve credentials at runtime without hardcoding
# any values in the notebook source.
# ─────────────────────────────────────────────────────────────────────────────

variable "sns_topic_arn" {
  description = "ARN of the SNS topic that receives forwarded gold.alerts messages"
  type        = string
}

variable "sns_publisher_access_key_id" {
  description = "IAM access key ID for the SNS publisher user — stored in Databricks Secrets"
  type        = string
}

variable "sns_publisher_secret_access_key" {
  description = "IAM access key secret for the SNS publisher user — stored in Databricks Secrets (sensitive)"
  type        = string
  sensitive   = true
}

variable "aws_region" {
  description = "AWS region where the SNS topic lives — used by the boto3 SNS client in the forwarding notebook"
  type        = string
  default     = "us-east-1"
}
