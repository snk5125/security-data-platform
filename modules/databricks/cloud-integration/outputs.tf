# -----------------------------------------------------------------------------
# Outputs — Cloud Integration Module
# -----------------------------------------------------------------------------
# Exposes storage credential names and external IDs consumed downstream:
#   - Credential names → Phase 6 (Unity Catalog managed location config)
#   - External IDs     → Phase 5.5 (IAM trust policy update with real values)
#   - External location URLs → Phase 8 (Auto Loader source paths)
# -----------------------------------------------------------------------------

output "hub_credential_name" {
  description = "Name of the hub storage credential — referenced by Unity Catalog external locations"
  value       = databricks_storage_credential.hub.name
}

output "managed_credential_name" {
  description = "Name of the managed storage credential — referenced by Unity Catalog managed tables"
  value       = databricks_storage_credential.managed.name
}

# External IDs assigned by Databricks when the storage credentials are created.
# These must be fed back into terraform.tfvars to update IAM trust policies
# in Phase 5.5 (two-pass apply pattern).
output "hub_credential_external_id" {
  description = "Databricks-assigned external ID for the hub credential — update terraform.tfvars with this value"
  value       = databricks_storage_credential.hub.aws_iam_role[0].external_id
}

output "managed_credential_external_id" {
  description = "Databricks-assigned external ID for the managed credential — update terraform.tfvars with this value"
  value       = databricks_storage_credential.managed.aws_iam_role[0].external_id
}

output "workload_a_external_location_url" {
  description = "S3 URL for workload A external location — used by Auto Loader in Phase 8"
  value       = databricks_external_location.workload_a.url
}

output "workload_b_external_location_url" {
  description = "S3 URL for workload B external location — used by Auto Loader in Phase 8"
  value       = databricks_external_location.workload_b.url
}

output "managed_external_location_url" {
  description = "S3 URL for managed storage external location"
  value       = databricks_external_location.managed.url
}
