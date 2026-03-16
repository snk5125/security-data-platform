# -----------------------------------------------------------------------------
# Input Variables — Workspace Configuration Module
# -----------------------------------------------------------------------------
# Parameterizes compute resources, cluster policies, and optional workspace
# features (SQL warehouse, git repo). Defaults are tuned for a PoC on a
# Databricks free trial workspace.
#
# Prerequisites:
#   - Phase 6 complete (catalog and schemas exist)
#   - Databricks workspace with active metastore
# -----------------------------------------------------------------------------

variable "catalog_name" {
  description = "Unity Catalog catalog name — set as the cluster's default catalog so notebooks don't need USE CATALOG"
  type        = string
  default     = "security_poc"
}

variable "enable_cluster" {
  description = "Whether to create a classic compute cluster. Set false if Free Edition (no classic compute support)"
  type        = bool
  default     = true
}

variable "enable_sql_warehouse" {
  description = "Whether to create a serverless SQL warehouse. Set false if free trial does not support serverless SQL"
  type        = bool
  default     = false
}

variable "git_repo_url" {
  description = "HTTPS URL of the git repository to connect to the workspace. Leave empty to skip git repo creation"
  type        = string
  default     = ""
}

variable "git_provider" {
  description = "Git provider type (gitHub, gitLab, bitbucketCloud, azureDevOpsServices, etc.)"
  type        = string
  default     = "gitHub"
}

variable "auto_termination_minutes" {
  description = "Minutes of inactivity before the cluster auto-terminates (cost control)"
  type        = number
  default     = 30
}
