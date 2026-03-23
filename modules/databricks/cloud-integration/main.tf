# -----------------------------------------------------------------------------
# Cloud Integration Module — Databricks ↔ AWS + Azure + GCP
# -----------------------------------------------------------------------------
# Connects Databricks Unity Catalog to AWS (and optionally Azure/GCP) by creating:
#
#   1. Storage credential (hub)     — wraps the hub IAM role for cross-account
#                                     access to security log buckets
#   2. Storage credential (managed) — wraps the managed storage role for Delta
#                                     table read/write
#   3. External locations (workloads)  — one per workload alias via for_each,
#                                        uses storage.url from workload manifest
#   4. Storage credential (azure)   — wraps an Entra ID service principal for
#                                      ADLS Gen2 access (conditional on Azure workloads)
#   5. External location (managed)    — s3://{managed-storage-bucket}/
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
# Resources created: 5 + (1 per workload in var.workloads) + (1 if Azure workloads exist) + (1 if GCP workloads exist)
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

locals {
  # Credential routing — maps cloud type to the correct storage credential name.
  # Uses merge with conditional entries to avoid null-reference errors when a
  # cloud type's credential doesn't exist.
  credential_by_cloud = merge(
    { "aws" = databricks_storage_credential.hub.name },
    var.azure_credentials != null ? { "azure" = databricks_storage_credential.azure[0].name } : {},
    var.gcp_credentials != null ? { "gcp" = databricks_storage_credential.gcp[0].name } : {},
  )

  # Azure diagnostic containers — Azure Monitor writes Activity Log data to an
  # auto-managed container named "insights-activity-logs", NOT to the workload's
  # primary "security-logs" container. Each Azure workload needs an additional
  # external location for this container so Databricks can read the diagnostic data.
  #
  # Extract the storage account host from the workload's abfss:// URL and construct
  # a new URL targeting the insights-activity-logs container.
  azure_diagnostic_locations = {
    for w in var.workloads : w.alias => {
      # abfss://security-logs@account.dfs.core.windows.net/
      #   → account.dfs.core.windows.net
      account_host = regex("@([^/]+)", w.storage.url)[0]
    }
    if w.cloud == "azure"
  }

}

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
  # Azure workloads use per-container external locations (activity-logs-*,
  # flow-logs-*) instead of a single workload-level location, because Azure
  # diagnostic data lands in system-managed containers, not a user-created one.
  for_each = { for w in var.workloads : w.alias => w if w.cloud != "azure" }

  name = "security-logs-${each.key}"
  url  = each.value.storage.url
  # Select credential by cloud type via lookup map: AWS → hub IAM role chain,
  # Azure → Entra ID service principal, GCP → service account key.
  credential_name = local.credential_by_cloud[each.value.cloud]
  read_only       = true
  comment         = "Security logs for ${each.key} (${each.value.cloud})"

  depends_on = [
    databricks_storage_credential.hub,
    databricks_storage_credential.azure,
    databricks_storage_credential.gcp,
  ]
}

# Azure diagnostic containers — Azure Monitor diagnostic settings write to
# auto-managed containers (e.g., "insights-activity-logs") that are separate
# from the workload's primary container. Each Azure workload needs an
# additional external location for Databricks to read this diagnostic data.
resource "databricks_external_location" "azure_diagnostic" {
  for_each = local.azure_diagnostic_locations

  name            = "activity-logs-${each.key}"
  url             = "abfss://insights-activity-logs@${each.value.account_host}/"
  credential_name = local.credential_by_cloud["azure"]
  read_only       = true
  comment         = "Azure Activity Log diagnostic data for ${each.key}"

  depends_on = [databricks_storage_credential.azure]
}

# Azure VNet flow log containers — Azure Network Watcher writes flow log data
# to an auto-managed container named "insights-logs-flowlogflowevent" on the
# same ADLS Gen2 storage account as Activity Log. Databricks reads via abfss://
# which requires HNS (enabled on the security_logs account).
# skip_validation: the container is created by Azure when the first flow log
# batch is written, which may take several minutes after the flow log resource
# is created. Databricks validates access at query time regardless.
resource "databricks_external_location" "azure_vnet_flow_log" {
  for_each = local.azure_diagnostic_locations

  name            = "flow-logs-${each.key}"
  url             = "abfss://insights-logs-flowlogflowevent@${each.value.account_host}/"
  credential_name = local.credential_by_cloud["azure"]
  read_only       = true
  skip_validation = true
  comment         = "Azure VNet Flow Log data for ${each.key}"

  depends_on = [databricks_storage_credential.azure]
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

# ═════════════════════════════════════════════════════════════════════════════
# 4. AZURE STORAGE CREDENTIAL (conditional)
# ═════════════════════════════════════════════════════════════════════════════
# Created only when Azure workloads exist. Uses an Entra ID service principal
# to authenticate Databricks to Azure ADLS Gen2 storage accounts. This is a
# third credential — completely separate from the AWS hub and managed credentials.

resource "databricks_storage_credential" "azure" {
  count = var.azure_credentials != null ? 1 : 0
  name  = "lakehouse-azure-credential"

  azure_service_principal {
    directory_id   = var.azure_credentials.directory_id
    application_id = var.azure_credentials.application_id
    client_secret  = var.azure_credentials.client_secret
  }

  comment = "Azure service principal credential for ADLS Gen2 security log access"
}

resource "databricks_grants" "azure_credential" {
  count              = var.azure_credentials != null ? 1 : 0
  storage_credential = databricks_storage_credential.azure[0].id

  grant {
    principal  = "account users"
    privileges = ["READ_FILES"]
  }
}

# ═════════════════════════════════════════════════════════════════════════════
# 5. GCP STORAGE CREDENTIAL (conditional)
# ═════════════════════════════════════════════════════════════════════════════
# Created only when GCP workloads exist. Uses a GCP service account key to
# authenticate Databricks to GCS. This is required for AWS-hosted workspaces
# (databricks_gcp_service_account only works on GCP-hosted workspaces).

resource "databricks_storage_credential" "gcp" {
  count = var.gcp_credentials != null ? 1 : 0
  name  = "lakehouse-gcp-credential"

  gcp_service_account_key {
    email          = var.gcp_credentials.client_email
    private_key_id = var.gcp_credentials.private_key_id
    private_key    = var.gcp_credentials.private_key
  }

  comment         = "GCP service account key credential for GCS security log access"
  skip_validation = true
}

resource "databricks_grants" "gcp_credential" {
  count              = var.gcp_credentials != null ? 1 : 0
  storage_credential = databricks_storage_credential.gcp[0].id

  grant {
    principal  = "account users"
    privileges = ["READ_FILES"]
  }
}
