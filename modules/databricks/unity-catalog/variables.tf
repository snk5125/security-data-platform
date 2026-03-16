# -----------------------------------------------------------------------------
# Input Variables — Unity Catalog Module
# -----------------------------------------------------------------------------
# Parameterizes the catalog name and managed storage credential. The module
# creates a medallion-architecture catalog (bronze/silver/gold schemas) with
# grants appropriate for a PoC environment.
#
# Prerequisites:
#   - Phase 5 complete (storage credentials and external locations exist)
#   - Databricks workspace has an active metastore (free trial provides one)
# -----------------------------------------------------------------------------

variable "catalog_name" {
  description = "Name of the Unity Catalog catalog to create (e.g., 'security_poc')"
  type        = string
  default     = "security_poc"
}

variable "managed_storage_bucket_name" {
  description = "Name of the managed storage S3 bucket — used as the catalog's storage root for managed tables"
  type        = string
}
