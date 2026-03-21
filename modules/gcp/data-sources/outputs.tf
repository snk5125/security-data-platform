output "bucket_name" {
  description = "Name of the GCS security logs bucket."
  value       = google_storage_bucket.security_logs.name
}

output "bucket_url" {
  description = "GCS URL for the security logs bucket (gs://bucket-name/)."
  value       = "gs://${google_storage_bucket.security_logs.name}/"
}

output "data_products" {
  description = "Map of data product names to format and path prefix."
  value = merge(
    {
      audit_logs = {
        format      = "json"
        path_prefix = "audit-logs/"
      }
      network_traffic = {
        format      = "json"
        path_prefix = "vpc-flow-logs/"
      }
      asset_inventory = {
        format      = "json"
        path_prefix = "asset-inventory/"
      }
    },
    var.enable_scc ? {
      scc_findings = {
        format      = "json"
        path_prefix = "scc-findings/"
      }
    } : {}
  )
}
