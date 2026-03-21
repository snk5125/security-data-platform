# -----------------------------------------------------------------------------
# Input Variables — Cloud Integration Module
# -----------------------------------------------------------------------------
# These variables connect Databricks storage credentials and external locations
# to the AWS IAM roles and S3 buckets created in Phases 2–4.
# -----------------------------------------------------------------------------

variable "hub_role_arn" {
  description = "ARN of the hub IAM role in the security account. Registered as a Databricks storage credential for cross-account security log access."
  type        = string
}

variable "managed_storage_role_arn" {
  description = "ARN of the managed storage IAM role. Registered as a Databricks storage credential for managed Delta table storage."
  type        = string
}

variable "managed_storage_bucket_name" {
  description = "Name of the managed storage S3 bucket in the security account (e.g., 'security-lakehouse-managed-123456')."
  type        = string
}

variable "workloads" {
  description = "List of workload manifests. Each entry describes one workload's cloud type, storage location, and access configuration."
  # Full type accepted for consistency with hub root, even if the module only
  # currently consumes alias, cloud, storage, and read_only_role_arn.
  type = list(object({
    cloud      = string # "aws", "azure", or "gcp"
    account_id = string
    alias      = string
    region     = string
    storage = object({
      type        = string
      url         = optional(string, "") # "s3://bucket/", "abfss://.../', or "gs://bucket/" — optional for backward compat
      bucket_name = optional(string, "")
      bucket_arn  = optional(string, "")
    })
    read_only_role_arn = optional(string, "")
    encryption = optional(object({
      type    = string
      key_arn = string
    }), { type = "none", key_arn = "" })
    data_products = map(object({
      format      = string
      path_prefix = string
    }))
  }))
}

variable "azure_credentials" {
  description = "Azure service principal for ADLS access. Null if no Azure workloads."
  type = object({
    directory_id   = string
    application_id = string
    client_secret  = string
  })
  default   = null
  sensitive = true
}

variable "gcp_credentials" {
  description = "GCP service account key for GCS access. Null if no GCP workloads."
  type = object({
    client_email   = string # maps to gcp_service_account_key.email
    private_key_id = string
    private_key    = string
  })
  default   = null
  sensitive = true
}
