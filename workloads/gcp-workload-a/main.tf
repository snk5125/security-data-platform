# Workload Root — GCP
# Deploys VPC, VMs, and security data sources in a GCP project.
# Combines workload-account-baseline and data-sources modules.

module "baseline" {
  source = "../../modules/gcp/workload-account-baseline"

  project_id  = var.project_id
  region      = var.region
  zone        = var.zone
  vpc_cidr    = var.vpc_cidr
  name_prefix = "${var.name_prefix}-${var.workload_alias}"
}

module "data_sources" {
  source = "../../modules/gcp/data-sources"

  project_id            = var.project_id
  region                = var.region
  name_prefix           = "${var.name_prefix}-${var.workload_alias}"
  service_account_email = var.service_account_email
  network_name          = module.baseline.network_name
  subnet_name           = module.baseline.subnet_name
  enable_scc            = var.enable_scc
  enable_host_telemetry = true
}
