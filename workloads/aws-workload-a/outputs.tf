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
      url         = "s3://${module.data_sources.security_logs_bucket_name}/"
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
    host_telemetry = {
      storage_url = module.data_sources.host_telemetry_storage_url
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

# VM access outputs — consumed by ansible/inventory/build-inventory.sh
# for Cribl Edge deployment to workload instances.
output "linux_public_ip" {
  description = "Public IP of the Linux instance."
  value       = module.baseline.linux_public_ip
}

output "windows_public_ip" {
  description = "Public IP of the Windows instance."
  value       = module.baseline.windows_public_ip
}

output "ssh_private_key" {
  description = "SSH private key for the Linux instance (sensitive, stored in state only)."
  value       = module.baseline.ssh_private_key
  sensitive   = true
}

output "windows_admin_password" {
  description = "Windows Administrator password (sensitive, stored in state only)."
  value       = module.baseline.windows_admin_password
  sensitive   = true
}
