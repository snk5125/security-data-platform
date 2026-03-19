# Foundation Root — Security Account
# Creates the managed storage bucket and SNS alert infrastructure in the
# security account. IAM roles are created by the hub root (which has access
# to Databricks-assigned external IDs).

module "security_foundation" {
  source = "../../modules/aws/security-foundation"

  security_account_id         = var.security_account_id
  organization_id             = var.organization_id
  managed_storage_bucket_name = var.managed_storage_bucket_name
}
