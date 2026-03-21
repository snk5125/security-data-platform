# Foundation Root — GCP Security
# Creates the service account and key for Databricks GCS access.
# Parallel to foundations/aws-security/ and foundations/azure-security/.

module "security_foundation" {
  source = "../../modules/gcp/security-foundation"

  project_id  = var.project_id
  region      = var.region
  name_prefix = var.name_prefix
}
