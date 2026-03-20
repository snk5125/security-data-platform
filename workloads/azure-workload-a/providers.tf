# Provider Configuration — Azure Workload Root
# Same subscription as the security foundation, resource-group isolation.

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}
