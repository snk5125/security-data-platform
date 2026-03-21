# -----------------------------------------------------------------------------
# GCP Security Foundation Module
# -----------------------------------------------------------------------------
# Creates the hub-level GCP resources for Databricks integration:
#   1. Enable required GCP APIs (storage, logging, asset inventory, scheduler)
#   2. Service Account for Databricks access
#   3. Service Account Key (JSON) — used as gcp_service_account_key in
#      Databricks storage credential
#
# The service account key is passed to the Databricks
# databricks_storage_credential resource in the hub root's cloud-integration
# module. Databricks uses it via the gcp_service_account_key credential type
# to access GCS from the AWS-hosted workspace.
#
# Prerequisites:
#   - gcloud CLI authenticated (`gcloud auth application-default login`)
#   - GCP project exists with billing enabled
# -----------------------------------------------------------------------------

# ═════════════════════════════════════════════════════════════════════════════
# 1. ENABLE REQUIRED APIS
# ═════════════════════════════════════════════════════════════════════════════
# These APIs must be enabled before any resources that depend on them can be
# created. The google_project_service resource is idempotent — re-applying
# when the API is already enabled is a no-op.

locals {
  required_apis = [
    "storage.googleapis.com",
    "logging.googleapis.com",
    "cloudasset.googleapis.com",
    "cloudscheduler.googleapis.com",
    "iam.googleapis.com",
    "compute.googleapis.com",
  ]
}

resource "google_project_service" "required" {
  for_each = toset(local.required_apis)

  project = var.project_id
  service = each.value

  # Don't disable the API when the resource is destroyed — other resources
  # in the project may depend on it.
  disable_on_destroy = false
}

# ═════════════════════════════════════════════════════════════════════════════
# 2. SERVICE ACCOUNT
# ═════════════════════════════════════════════════════════════════════════════
# Creates a dedicated service account for Databricks to authenticate to GCS.
# The key is generated below and passed to the hub root.

resource "google_service_account" "databricks" {
  project      = var.project_id
  account_id   = "${var.name_prefix}-databricks-sa"
  display_name = "Lakehouse Databricks Service Account"
  description  = "Service account for Databricks Unity Catalog access to GCS security logs"

  depends_on = [google_project_service.required]
}

# ═════════════════════════════════════════════════════════════════════════════
# 3. SERVICE ACCOUNT KEY
# ═════════════════════════════════════════════════════════════════════════════
# Generates a JSON key for the service account. This key is base64-encoded
# and contains the fields needed by Databricks' gcp_service_account_key block:
#   - client_email (maps to email)
#   - private_key_id
#   - private_key
#
# IMPORTANT: The decoded JSON is sensitive and should never be committed.
# It flows from this output → hub/terraform.tfvars → cloud-integration module.

resource "google_service_account_key" "databricks" {
  service_account_id = google_service_account.databricks.name
}
