# -----------------------------------------------------------------------------
# GCP Data Sources Module
# -----------------------------------------------------------------------------
# Creates security log collection infrastructure for a GCP workload:
#   1. GCS bucket for security logs (prefixed paths per data source)
#   2. Cloud Audit Logs log sink → GCS (audit-logs/ prefix)
#   3. VPC Flow Logs log sink → GCS (vpc-flow-logs/ prefix)
#      Note: Flow log collection is enabled at the subnet level (baseline module
#      log_config block). This module exports those Cloud Logging entries to GCS.
#   4. Cloud Asset Inventory scheduled export → GCS (asset-inventory/ prefix)
#   5. SCC Findings export (conditional) → GCS (scc-findings/ prefix)
#   6. IAM bindings — SA gets objectViewer + legacyBucketReader (both required
#      for Databricks access via gcp_service_account_key)
#
# Mirrors the Azure data-sources module pattern. The Databricks service account
# from the security foundation gets read-only access to the logs bucket.
#
# Prerequisites:
#   - Workload baseline applied (VPC and subnet with log_config exist)
#   - Security foundation applied (service account exists, APIs enabled)
# -----------------------------------------------------------------------------

# ═════════════════════════════════════════════════════════════════════════════
# 1. GCS BUCKET — Security Logs
# ═════════════════════════════════════════════════════════════════════════════
# Single bucket with prefixed paths per data source. Mirrors the Azure pattern
# of one storage account per workload with containers per data source.

resource "google_storage_bucket" "security_logs" {
  project       = var.project_id
  name          = "${var.name_prefix}-security-logs"
  location      = var.region
  force_destroy = true # Demo — allows terraform destroy without emptying

  uniform_bucket_level_access = true
}

# ═════════════════════════════════════════════════════════════════════════════
# 2. IAM BINDINGS — Databricks SA read access
# ═════════════════════════════════════════════════════════════════════════════
# Databricks requires BOTH roles for GCS access:
#   - roles/storage.objectViewer — read objects
#   - roles/storage.legacyBucketReader — provides storage.buckets.get which
#     Databricks validates when creating external locations

resource "google_storage_bucket_iam_member" "sa_object_viewer" {
  bucket = google_storage_bucket.security_logs.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${var.service_account_email}"
}

resource "google_storage_bucket_iam_member" "sa_legacy_reader" {
  bucket = google_storage_bucket.security_logs.name
  role   = "roles/storage.legacyBucketReader"
  member = "serviceAccount:${var.service_account_email}"
}

# ═════════════════════════════════════════════════════════════════════════════
# 3. CLOUD AUDIT LOGS — Log Sink to GCS
# ═════════════════════════════════════════════════════════════════════════════
# Routes admin activity and data access audit logs to GCS. The log sink's
# auto-generated writer identity needs objectCreator permissions on the bucket.

resource "google_logging_project_sink" "audit_logs" {
  project = var.project_id
  name    = "${var.name_prefix}-audit-logs-sink"
  # GCP log sinks only accept bucket-level destinations (no subpath).
  # Logs are organized within the bucket by Cloud Logging's auto-generated
  # path structure: cloudaudit.googleapis.com/activity/YYYY/MM/DD/...
  destination = "storage.googleapis.com/${google_storage_bucket.security_logs.name}"

  # Admin Activity logs are always-on and free. Data Access logs may need
  # explicit enablement at the project level.
  filter = "logName:\"logs/cloudaudit.googleapis.com\""

  # Create a unique writer identity for this sink.
  unique_writer_identity = true
}

# Grant the sink's writer identity permission to write to the bucket.
resource "google_storage_bucket_iam_member" "audit_logs_writer" {
  bucket = google_storage_bucket.security_logs.name
  role   = "roles/storage.objectCreator"
  member = google_logging_project_sink.audit_logs.writer_identity
}

# ═════════════════════════════════════════════════════════════════════════════
# 4. VPC FLOW LOGS — Log Sink to GCS
# ═════════════════════════════════════════════════════════════════════════════
# Two-module coordination:
#   - Baseline module: enables flow log collection on the subnet (log_config)
#   - This module: exports those Cloud Logging entries to GCS via log sink
# This is analogous to Azure where VNet Flow Logs are enabled on the VNet
# and exported to a storage container.

resource "google_logging_project_sink" "vpc_flow_logs" {
  project = var.project_id
  name    = "${var.name_prefix}-vpc-flow-logs-sink"
  # GCP log sinks only accept bucket-level destinations (no subpath).
  # VPC flow logs are organized by Cloud Logging's auto-generated path.
  destination = "storage.googleapis.com/${google_storage_bucket.security_logs.name}"

  filter = "resource.type=\"gce_subnetwork\" AND logName:\"logs/compute.googleapis.com%2Fvpc_flows\""

  unique_writer_identity = true
}

resource "google_storage_bucket_iam_member" "vpc_flow_logs_writer" {
  bucket = google_storage_bucket.security_logs.name
  role   = "roles/storage.objectCreator"
  member = google_logging_project_sink.vpc_flow_logs.writer_identity
}

# ═════════════════════════════════════════════════════════════════════════════
# 5. CLOUD ASSET INVENTORY — Scheduled Daily Export
# ═════════════════════════════════════════════════════════════════════════════
# Uses Cloud Scheduler to trigger a daily exportAssets API call to GCS.
# This is the simpler alternative to Pub/Sub + Cloud Function and matches
# the AWS Config daily snapshot cadence.
#
# The Databricks SA is reused for the Scheduler oauth_token — it needs
# the cloudasset.assets.exportResource permission (granted via
# roles/cloudasset.owner) and storage.objects.create on the bucket.

resource "google_cloud_scheduler_job" "asset_export" {
  project     = var.project_id
  name        = "${var.name_prefix}-asset-export-daily"
  region      = var.region
  description = "Daily Cloud Asset Inventory export to GCS"
  schedule    = "0 2 * * *" # Daily at 02:00 UTC

  http_target {
    http_method = "POST"
    uri         = "https://cloudasset.googleapis.com/v1/projects/${var.project_id}:exportAssets"
    body = base64encode(jsonencode({
      outputConfig = {
        gcsDestination = {
          uriPrefix = "gs://${google_storage_bucket.security_logs.name}/asset-inventory"
        }
      }
      contentType = "RESOURCE"
    }))
    headers = {
      "Content-Type" = "application/json"
    }

    oauth_token {
      service_account_email = var.service_account_email
    }
  }
}

# Grant the SA permission to export assets.
resource "google_project_iam_member" "sa_asset_export" {
  project = var.project_id
  role    = "roles/cloudasset.owner"
  member  = "serviceAccount:${var.service_account_email}"
}

resource "google_storage_bucket_iam_member" "sa_object_creator" {
  bucket = google_storage_bucket.security_logs.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${var.service_account_email}"
}

# The Cloud Asset API uses its own service agent (not the caller's SA)
# to write export files to GCS. This service agent needs write access
# on the destination bucket.
data "google_project" "current" {
  project_id = var.project_id
}

resource "google_storage_bucket_iam_member" "asset_service_agent" {
  bucket = google_storage_bucket.security_logs.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-cloudasset.iam.gserviceaccount.com"
}

# ═════════════════════════════════════════════════════════════════════════════
# 6. SCC FINDINGS (conditional)
# ═════════════════════════════════════════════════════════════════════════════
# Only created when enable_scc = true. Requires org-level SCC Standard
# activation. Creates a notification config that routes findings to Pub/Sub,
# which exports to GCS.

# Placeholder — SCC requires organization-level resources. When enable_scc
# is true, add google_scc_notification_config and Pub/Sub → GCS export.
# Deferred until org setup is complete.
