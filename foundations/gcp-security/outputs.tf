output "service_account_email" {
  description = "Databricks service account email."
  value       = module.security_foundation.service_account_email
}

output "service_account_private_key" {
  description = "Base64-encoded service account key JSON."
  value       = module.security_foundation.service_account_private_key
  sensitive   = true
}

output "project_id" {
  description = "GCP project ID."
  value       = module.security_foundation.project_id
}
