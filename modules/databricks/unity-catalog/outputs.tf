# -----------------------------------------------------------------------------
# Outputs — Unity Catalog Module
# -----------------------------------------------------------------------------
# Exposes catalog and schema names consumed by downstream phases:
#   - Catalog name → Phase 8 (Auto Loader notebook table references)
#   - Schema names → Phase 8 (target schemas for bronze ingest)
# -----------------------------------------------------------------------------

output "catalog_name" {
  description = "Name of the Unity Catalog catalog — used as namespace prefix for all tables"
  value       = databricks_catalog.this.name
}

output "bronze_schema_name" {
  description = "Name of the bronze schema — raw ingest target for Auto Loader"
  value       = databricks_schema.bronze.name
}

output "silver_schema_name" {
  description = "Name of the silver schema — normalized event target"
  value       = databricks_schema.silver.name
}

output "gold_schema_name" {
  description = "Name of the gold schema — analytical products target"
  value       = databricks_schema.gold.name
}

output "extra_schema_names" {
  description = "Map of extra schema names."
  value       = { for k, v in databricks_schema.extra : k => v.name }
}
