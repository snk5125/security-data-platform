# Outputs — GCP Security Foundation Module
# These values are consumed by:
#   - hub root: gcp_credentials variable (extracted from decoded key JSON)
#   - workload roots: service_account_email for IAM bindings on log storage

output "service_account_email" {
  description = "Email of the Databricks service account — used for IAM bindings in workload roots."
  value       = google_service_account.databricks.email
}

output "service_account_private_key" {
  description = "Base64-encoded service account key JSON. Decode to extract client_email, private_key_id, private_key for hub gcp_credentials."
  value       = google_service_account_key.databricks.private_key
  sensitive   = true
}

output "project_id" {
  description = "GCP project ID (pass-through)."
  value       = var.project_id
}
