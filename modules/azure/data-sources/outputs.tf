output "storage_account_name" {
  description = "Name of the security logs ADLS Gen2 storage account."
  value       = azurerm_storage_account.security_logs.name
}

output "storage_account_id" {
  description = "Resource ID of the security logs storage account."
  value       = azurerm_storage_account.security_logs.id
}

