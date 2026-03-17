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

variable "workload_a_security_logs_bucket_name" {
  description = "Workload A security logs S3 bucket name — source for Auto Loader"
  type        = string
}

variable "workload_b_security_logs_bucket_name" {
  description = "Workload B security logs S3 bucket name — source for Auto Loader"
  type        = string
}

variable "notebook_source_dir" {
  description = "Local path to the bronze notebook source files (relative to the root module)"
  type        = string
  default     = "../../notebooks/bronze"
}

variable "workspace_notebook_path" {
  description = "Workspace path prefix where bronze notebooks are uploaded"
  type        = string
  default     = "/Shared/security-lakehouse/bronze"
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
