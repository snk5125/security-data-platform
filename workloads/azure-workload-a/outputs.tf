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
        path_prefix = "vnet-flow-logs/"
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
