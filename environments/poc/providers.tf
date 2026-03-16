# -----------------------------------------------------------------------------
# Provider Configuration — PoC Environment
# -----------------------------------------------------------------------------
# Defines four providers for the multi-account + Databricks deployment:
#
#   1. aws (default)      — security/management account (<SECURITY_ACCOUNT_ID>)
#   2. aws.workload_a     — workload account A via OrganizationAccountAccessRole
#   3. aws.workload_b     — workload account B via OrganizationAccountAccessRole
#   4. databricks          — workspace-level (used starting Phase 5)
#
# The default AWS provider authenticates using the caller's existing
# credentials (IAM user, SSO session, or instance profile). The aliased
# providers chain-assume into child accounts using the role that AWS
# Organizations creates automatically in each member account.
#
# The Databricks provider is declared here but only initialized when
# resources reference it. It is safe to declare with empty defaults —
# Terraform will not attempt to connect until Phase 5 resources are added.
# -----------------------------------------------------------------------------

# ── Default provider: security/management account ─────────────────────────────
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

# ── Workload account A ────────────────────────────────────────────────────────
# Assumes the OrganizationAccountAccessRole in workload account A. This role
# is created automatically by AWS Organizations when the member account is
# created and grants AdministratorAccess to the management account.
provider "aws" {
  alias  = "workload_a"
  region = var.aws_region

  assume_role {
    role_arn = "arn:aws:iam::${var.workload_a_account_id}:role/OrganizationAccountAccessRole"
  }

  default_tags {
    tags = local.common_tags
  }
}

# ── Workload account B ────────────────────────────────────────────────────────
# Same pattern as workload A — assumes into the second member account.
provider "aws" {
  alias  = "workload_b"
  region = var.aws_region

  assume_role {
    role_arn = "arn:aws:iam::${var.workload_b_account_id}:role/OrganizationAccountAccessRole"
  }

  default_tags {
    tags = local.common_tags
  }
}

# ── Databricks workspace provider ────────────────────────────────────────────
# Workspace-level only (no account-level API on the free trial). This provider
# is used starting in Phase 5 when Unity Catalog resources are created. It is
# safe to declare here with empty defaults because Terraform only initializes
# a provider when at least one resource or data source references it.
provider "databricks" {
  host  = var.databricks_workspace_url
  token = var.databricks_pat
}
