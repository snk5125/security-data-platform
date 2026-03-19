# Outputs — Workload Contract
# Every workload root exports a standardized JSON manifest consumed by
# assemble-workloads.sh → hub/workloads.auto.tfvars.json.

output "workload_manifest" {
  description = "Standardized workload output contract for hub consumption."
  value = {
    cloud      = "aws"
    account_id = var.account_id
    alias      = var.account_alias
    region     = var.aws_region
    storage = {
      type        = "s3"
      bucket_name = module.data_sources.security_logs_bucket_name
      bucket_arn  = module.data_sources.security_logs_bucket_arn
    }
    read_only_role_arn = module.data_sources.read_only_role_arn
    encryption = {
      type    = "kms"
      key_arn = module.data_sources.kms_key_arn
    }
    data_products = {
      network_traffic = {
        format      = "json"
        path_prefix = "vpc-flow-logs/"
      }
      management_plane = {
        format      = "json"
        path_prefix = "cloudtrail/"
      }
      threat_detection = {
        format      = "json"
        path_prefix = "guardduty/"
      }
      resource_inventory = {
        format      = "json"
        path_prefix = "config/"
      }
    }
  }
}

# Pass-through outputs for convenience / debugging.
output "vpc_id" {
  description = "Workload VPC ID."
  value       = module.baseline.vpc_id
}

output "security_logs_bucket_name" {
  description = "Security logs S3 bucket name."
  value       = module.data_sources.security_logs_bucket_name
}
