# Workload Root — AWS Account
# Deploys VPC, EC2, and security data sources in a single workload account.
# Combines workload-account-baseline and data-sources modules.

module "baseline" {
  source = "../../modules/aws/workload-account-baseline"

  account_alias      = var.account_alias
  account_id         = var.account_id
  vpc_cidr           = var.vpc_cidr
  public_subnet_cidr = var.public_subnet_cidr
}

module "data_sources" {
  source = "../../modules/aws/data-sources"

  account_alias = var.account_alias
  account_id    = var.account_id
  region        = var.aws_region
  vpc_id        = module.baseline.vpc_id
  # Deterministic hub role ARN — the role may not exist yet (created by hub root
  # in Step 4), but ARN-based trust policies don't require the principal to exist.
  hub_role_arn = "arn:aws:iam::${var.security_account_id}:role/${var.hub_role_name}"
}
