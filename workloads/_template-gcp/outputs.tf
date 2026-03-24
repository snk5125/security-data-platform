# Outputs — GCP Workload Contract
# Every workload root exports a standardized JSON manifest consumed by
# assemble-workloads.sh → hub/workloads.auto.tfvars.json.
# GCP workloads omit AWS-specific fields — they default to empty via optional().

output "workload_manifest" {
  description = "Standardized workload output contract for hub consumption."
  value = {
    cloud      = "gcp"
    account_id = var.project_id
    alias      = var.workload_alias
    region     = var.region
    storage = {
      type = "gcs"
      url  = module.data_sources.bucket_url
    }
    data_products = module.data_sources.data_products
  }
}

output "network_name" {
  description = "Workload VPC network name."
  value       = module.baseline.network_name
}

output "bucket_name" {
  description = "Security logs GCS bucket name."
  value       = module.data_sources.bucket_name
}

# VM access outputs — consumed by ansible/inventory/build-inventory.sh
# for Cribl Edge deployment to workload instances.
# GCP baseline uses linux_vm_ip / windows_vm_ip (not linux_public_ip).
output "linux_vm_ip" {
  description = "External IP of the Linux VM."
  value       = module.baseline.linux_vm_ip
}

output "windows_vm_ip" {
  description = "External IP of the Windows VM."
  value       = module.baseline.windows_vm_ip
}

output "ssh_private_key" {
  description = "SSH private key for the Linux VM (sensitive, stored in state only)."
  value       = module.baseline.ssh_private_key
  sensitive   = true
}
