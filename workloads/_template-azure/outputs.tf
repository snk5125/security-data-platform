# Outputs — Azure Workload Contract
# Every workload root exports a standardized JSON manifest consumed by
# assemble-workloads.sh → hub/workloads.auto.tfvars.json.
# Azure workloads omit AWS-specific fields — they default to empty via optional().

output "workload_manifest" {
  description = "Standardized workload output contract for hub consumption."
  value = {
    cloud      = "azure"
    account_id = var.subscription_id
    alias      = var.workload_alias
    region     = var.location
    storage = {
      type = "adls"
      url  = "abfss://security-logs@${module.data_sources.storage_account_name}.dfs.core.windows.net/"
      # bucket_name and bucket_arn omitted — default to "" via optional()
    }
    # read_only_role_arn omitted — defaults to "" via optional()
    # encryption omitted — defaults to { type = "none", key_arn = "" } via optional()
    data_products = {
      network_traffic = {
        format      = "json"
        path_prefix = "insights-logs-flowlogflowevent/"
      }
      management_plane = {
        format      = "json"
        path_prefix = "insights-activity-logs/"
      }
    }
  }
}

output "vnet_id" {
  description = "Workload VNet ID."
  value       = module.baseline.vnet_id
}

output "storage_account_name" {
  description = "Security logs ADLS Gen2 storage account name."
  value       = module.data_sources.storage_account_name
}

# VM access outputs — consumed by ansible/inventory/build-inventory.sh
# for Cribl Edge deployment to workload instances.
output "linux_public_ip" {
  description = "Public IP of the Linux VM."
  value       = module.baseline.linux_public_ip
}

output "windows_public_ip" {
  description = "Public IP of the Windows VM."
  value       = module.baseline.windows_public_ip
}

output "ssh_private_key" {
  description = "SSH private key for the Linux VM (sensitive, stored in state only)."
  value       = module.baseline.ssh_private_key
  sensitive   = true
}
