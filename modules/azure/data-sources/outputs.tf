output "storage_account_name" {
  description = "Name of the security logs ADLS Gen2 storage account."
  value       = azurerm_storage_account.security_logs.name
}

output "storage_account_id" {
  description = "Resource ID of the security logs storage account."
  value       = azurerm_storage_account.security_logs.id
}

# --- Host Telemetry Outputs (conditional) ---

output "host_telemetry_storage_url" {
  description = "ADLS Gen2 URL for the host telemetry container — used for Databricks external locations and Auto Loader. Empty string when host telemetry is disabled."
  value       = var.enable_host_telemetry ? "abfss://host-telemetry@${azurerm_storage_account.security_logs.name}.dfs.core.windows.net/" : ""
}

