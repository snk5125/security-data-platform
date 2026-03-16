# -----------------------------------------------------------------------------
# Unity Catalog Module — Catalog, Schemas, and Grants
# -----------------------------------------------------------------------------
# Creates the data governance layer for the security data lakehouse:
#
#   1. Catalog (security_poc)     — top-level namespace for all security data
#   2. Schema  (bronze)           — raw, immutable ingest layer (Auto Loader)
#   3. Schema  (silver)           — normalized, typed, query-optimized events
#   4. Schema  (gold)             — analytical products, aggregations, detections
#   5. Grants  (catalog)          — USE CATALOG for account users
#   6. Grants  (bronze/silver/gold) — full schema access for account users
#
# The medallion architecture (bronze -> silver -> gold) is a standard Databricks
# pattern for data quality tiers. Security telemetry lands raw in bronze,
# is cleaned/normalized in silver, and aggregated for analysis in gold.
#
# Grants use "account users" principal — appropriate for a PoC. Production
# deployments should scope to specific groups or service principals.
#
# Prerequisites:
#   - Free trial metastore auto-assigned to workspace (no explicit creation)
#   - Phase 5 storage credentials and external locations verified
#
# Resources created: 8
# -----------------------------------------------------------------------------

# ═════════════════════════════════════════════════════════════════════════════
# 1. CATALOG
# ═════════════════════════════════════════════════════════════════════════════
# The catalog is the top-level namespace in Unity Catalog. All schemas, tables,
# and views live under it. The free trial metastore has "Default Storage" enabled
# but no storage root URL configured, so we must provide an explicit storage_root
# pointing to a location under the managed storage external location (Phase 5).
# Managed tables in this catalog will be stored under this S3 path.

resource "databricks_catalog" "this" {
  name         = var.catalog_name
  comment      = "Security data lakehouse PoC — multi-account AWS security telemetry"
  storage_root = "s3://${var.managed_storage_bucket_name}/catalog/${var.catalog_name}"

  # Force clean destroy: drop all schemas/tables when the catalog is destroyed.
  # Safe for a PoC; production would use lifecycle prevent_destroy instead.
  force_destroy = true
}

# ═════════════════════════════════════════════════════════════════════════════
# 2. SCHEMAS (Medallion Architecture)
# ═════════════════════════════════════════════════════════════════════════════
# Three schemas following the medallion pattern. Each schema inherits its
# managed storage location from the catalog (which inherits from the metastore).

# Bronze — raw ingest layer. Auto Loader writes CloudTrail, VPC Flow Logs,
# GuardDuty findings, and AWS Config snapshots here as-is from S3.
resource "databricks_schema" "bronze" {
  catalog_name = databricks_catalog.this.name
  name         = "bronze"
  comment      = "Raw, immutable ingest layer — Auto Loader writes here from S3 external locations"

  force_destroy = true
}

# Silver — normalized layer. Structured transformations parse JSON, flatten
# nested fields, cast types, and deduplicate events.
resource "databricks_schema" "silver" {
  catalog_name = databricks_catalog.this.name
  name         = "silver"
  comment      = "Normalized, typed, query-optimized security events"

  force_destroy = true
}

# Gold — analytical layer. Aggregations, joins across data sources, detection
# rule outputs, and dashboard-ready materialized views live here.
resource "databricks_schema" "gold" {
  catalog_name = databricks_catalog.this.name
  name         = "gold"
  comment      = "Analytical products — aggregations, detection outputs, dashboard views"

  force_destroy = true
}

# ═════════════════════════════════════════════════════════════════════════════
# 3. GRANTS
# ═════════════════════════════════════════════════════════════════════════════
# PoC grants give all workspace users access to the catalog and schemas.
# Unity Catalog requires both catalog-level USE CATALOG and schema-level
# USE SCHEMA + CREATE TABLE for users to work with tables.

# Catalog-level: USE CATALOG + CREATE SCHEMA allows all users to browse
# the catalog and create new schemas if needed.
resource "databricks_grants" "catalog" {
  catalog = databricks_catalog.this.name

  grant {
    principal  = "account users"
    privileges = ["USE_CATALOG", "CREATE_SCHEMA"]
  }
}

# Schema-level grants: USE SCHEMA + CREATE TABLE + CREATE FUNCTION allows
# all users to create and query tables within each schema.

resource "databricks_grants" "bronze" {
  schema = "${databricks_catalog.this.name}.${databricks_schema.bronze.name}"

  grant {
    principal  = "account users"
    privileges = ["USE_SCHEMA", "CREATE_TABLE", "CREATE_FUNCTION"]
  }
}

resource "databricks_grants" "silver" {
  schema = "${databricks_catalog.this.name}.${databricks_schema.silver.name}"

  grant {
    principal  = "account users"
    privileges = ["USE_SCHEMA", "CREATE_TABLE", "CREATE_FUNCTION"]
  }
}

resource "databricks_grants" "gold" {
  schema = "${databricks_catalog.this.name}.${databricks_schema.gold.name}"

  grant {
    principal  = "account users"
    privileges = ["USE_SCHEMA", "CREATE_TABLE", "CREATE_FUNCTION"]
  }
}
