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
