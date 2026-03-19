# -----------------------------------------------------------------------------
# Cloud Integration Module — Databricks ↔ AWS
# -----------------------------------------------------------------------------
# Connects Databricks Unity Catalog to AWS by creating:
#
#   1. Storage credential (hub)     — wraps the hub IAM role for cross-account
#                                     access to security log buckets
#   2. Storage credential (managed) — wraps the managed storage role for Delta
#                                     table read/write
#   3. External locations (workloads)  — one per workload alias via for_each,
#                                        s3://{workload-security-logs}/
#   4. External location (managed)    — s3://{managed-storage-bucket}/
#   6. Grants on hub credential     — READ_FILES for account users
#   7. Grants on managed credential — READ_FILES + WRITE_FILES for account users
#
# IAM access chain for security logs:
#   Databricks UC master role → hub storage credential (this module)
#     → hub IAM role (Phase 2) → chain-assume → workload read-only role (Phase 4)
#       → S3 GetObject/ListBucket on security-logs bucket
#
# Prerequisites:
#   - Phase 2 complete (hub role + managed storage role exist)
#   - Phase 4 complete (security-logs buckets + read-only roles exist)
#   - Databricks workspace provisioned with PAT authentication
#
# After apply:
#   - Output the external IDs assigned by Databricks
#   - Feed them back into terraform.tfvars for Phase 5.5 trust policy update
#
# Resources created: 5 + (1 per workload in var.workloads)
# -----------------------------------------------------------------------------

# ═════════════════════════════════════════════════════════════════════════════
# 1. STORAGE CREDENTIALS
# ═════════════════════════════════════════════════════════════════════════════
# Storage credentials register AWS IAM roles with Databricks so that Unity
# Catalog can assume them when accessing S3 data. Each credential wraps one
# IAM role and receives a Databricks-assigned external ID that must be added
# to the role's trust policy (Phase 5.5).

# Hub credential — used by external locations that point to workload account
# security log buckets. Databricks assumes this role, which then chain-assumes
# into the per-account read-only roles created in Phase 4.
resource "databricks_storage_credential" "hub" {
  name = "lakehouse-hub-credential"

  aws_iam_role {
    role_arn = var.hub_role_arn
  }

  comment = "Hub credential for cross-account security log access via IAM role chain"
}

# Managed storage credential — used by the external location that points to
# the managed Delta table bucket in the security account. Databricks uses
# this for all managed table read/write operations.
resource "databricks_storage_credential" "managed" {
  name = "lakehouse-managed-credential"

  aws_iam_role {
    role_arn = var.managed_storage_role_arn
  }

  comment = "Credential for managed Delta table storage in the security account"
}

# ═════════════════════════════════════════════════════════════════════════════
# 2. EXTERNAL LOCATIONS
# ═════════════════════════════════════════════════════════════════════════════
# External locations map S3 paths to storage credentials so that Unity Catalog
# knows which IAM role to assume when reading from or writing to each bucket.
# Each location must reference its credential by name (not ARN).

# One external location per workload, keyed by alias (e.g., "workload_a",
# "workload_b"). Each points to the workload's security-logs S3 bucket and
# uses the hub credential for cross-account IAM role chain access.
#
# Read-only: the hub role intentionally has only S3 read permissions on
# workload buckets. Without this flag, Databricks validates WRITE+DELETE
# and rejects the location. Security logs are written by AWS services,
# not Databricks.
#
# Explicit dependency: the credential must be fully registered before
# Databricks can validate each external location's S3 access.
resource "databricks_external_location" "workload" {
  for_each = { for w in var.workloads : w.alias => w }

  name            = "security-logs-${each.key}"
  url             = "s3://${each.value.storage.bucket_name}/"
  credential_name = databricks_storage_credential.hub.name
  read_only       = true
  comment         = "Security logs for ${each.key} (${each.value.cloud})"

  depends_on = [databricks_storage_credential.hub]
}

# Managed storage — Databricks writes managed Delta tables here. This is the
# bucket created in Phase 2 in the security account.
resource "databricks_external_location" "managed" {
  name            = "managed-storage"
  url             = "s3://${var.managed_storage_bucket_name}/"
  credential_name = databricks_storage_credential.managed.name
  comment         = "Managed Delta table storage in the security account"

  depends_on = [databricks_storage_credential.managed]
}

# ═════════════════════════════════════════════════════════════════════════════
# 3. GRANTS
# ═════════════════════════════════════════════════════════════════════════════
# Grant permissions on storage credentials so that workspace users can use
# them via external locations and Unity Catalog. The "account users" group
# includes all users in the workspace — appropriate for a PoC. Production
# deployments should scope grants to specific groups or service principals.

# Hub credential grants — READ_FILES only because security logs are read-only
# from the Databricks perspective (data is written by AWS services).
resource "databricks_grants" "hub_credential" {
  storage_credential = databricks_storage_credential.hub.id

  grant {
    principal  = "account users"
    privileges = ["READ_FILES"]
  }
}

# Managed credential grants — READ_FILES + WRITE_FILES because Databricks
# needs to both read and write managed Delta tables.
resource "databricks_grants" "managed_credential" {
  storage_credential = databricks_storage_credential.managed.id

  grant {
    principal  = "account users"
    privileges = ["READ_FILES", "WRITE_FILES"]
  }
}
