# Provider Configuration — Azure Security Foundation
# Uses Azure CLI authentication by default. For CI/CD, set ARM_CLIENT_ID,
# ARM_CLIENT_SECRET, ARM_TENANT_ID environment variables for service
# principal authentication.

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

provider "azuread" {
  # Uses same authentication as azurerm (Azure CLI or env vars).
}
