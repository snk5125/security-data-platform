# Outputs — Azure Security Foundation Module
# These values are consumed by:
#   - hub root: azure_credentials variable (directory_id, application_id, client_secret)
#   - workload roots: service_principal_id for role assignments on log storage

output "directory_id" {
  description = "Entra ID (Azure AD) tenant/directory ID."
  value       = data.azuread_client_config.current.tenant_id
}

output "application_id" {
  description = "Entra ID application (client) ID for the Databricks service principal."
  value       = azuread_application.databricks.client_id
}

output "client_secret" {
  description = "Client secret for the Databricks service principal."
  value       = azuread_application_password.databricks.value
  sensitive   = true
}

output "service_principal_id" {
  description = "Object ID of the Databricks service principal — used for role assignments in workload roots."
  value       = azuread_service_principal.databricks.object_id
}

output "storage_account_name" {
  description = "Name of the managed ADLS Gen2 storage account."
  value       = azurerm_storage_account.managed.name
}

output "resource_group_name" {
  description = "Name of the security hub resource group."
  value       = azurerm_resource_group.security_hub.name
}
