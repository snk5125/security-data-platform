# Foundation Root — Azure Security
# Creates the Entra ID service principal and managed ADLS Gen2 storage for
# Databricks integration. Parallel to foundations/aws-security/.

module "security_foundation" {
  source = "../../modules/azure/security-foundation"

  subscription_id = var.subscription_id
  location        = var.location
  name_prefix     = var.name_prefix
}
