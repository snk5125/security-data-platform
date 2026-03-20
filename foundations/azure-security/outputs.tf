output "directory_id" {
  description = "Entra ID tenant/directory ID."
  value       = module.security_foundation.directory_id
}

output "application_id" {
  description = "Entra ID application (client) ID."
  value       = module.security_foundation.application_id
}

output "client_secret" {
  description = "Service principal client secret."
  value       = module.security_foundation.client_secret
  sensitive   = true
}

output "service_principal_id" {
  description = "Service principal object ID."
  value       = module.security_foundation.service_principal_id
}

output "storage_account_name" {
  description = "Managed ADLS Gen2 storage account name."
  value       = module.security_foundation.storage_account_name
}

output "resource_group_name" {
  description = "Security hub resource group name."
  value       = module.security_foundation.resource_group_name
}
