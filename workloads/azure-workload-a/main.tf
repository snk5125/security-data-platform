# Workload Root — Azure
# Deploys VNet, VMs, and security data sources in an Azure resource group.
# Combines workload-account-baseline and data-sources modules.

module "baseline" {
  source = "../../modules/azure/workload-account-baseline"

  resource_group_name = "${var.name_prefix}-rg-${var.workload_alias}"
  location            = var.location
  vnet_cidr           = var.vnet_cidr
  subnet_cidr         = var.subnet_cidr
  name_prefix         = "${var.name_prefix}-${var.workload_alias}"
}

module "data_sources" {
  source = "../../modules/azure/data-sources"

  resource_group_name  = module.baseline.resource_group_name
  location             = var.location
  subscription_id      = var.subscription_id
  vnet_id              = module.baseline.vnet_id
  nsg_id               = module.baseline.nsg_id
  name_prefix          = "${var.name_prefix}-${var.workload_alias}"
  service_principal_id = var.service_principal_id
}
