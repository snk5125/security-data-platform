# -----------------------------------------------------------------------------
# Workspace Configuration Module — Compute, Policies, and Git Integration
# -----------------------------------------------------------------------------
# Configures the Databricks workspace with cost-controlled compute resources:
#
#   1. Data sources      — resolve latest LTS runtime and smallest node type
#   2. Cluster policy    — enforces cost controls (node type, workers, auto-term)
#   3. Cluster           — single-node PoC cluster with Unity Catalog enabled
#   4. SQL warehouse     — optional serverless SQL endpoint for ad-hoc queries
#   5. Git repo          — optional workspace repo integration
#
# The cluster uses SINGLE_USER data security mode, which is required for
# single-node clusters with Unity Catalog. The current user is automatically
# set as the single user.
#
# Prerequisites:
#   - Phase 6 complete (catalog and schemas exist for default catalog setting)
#   - Free trial must be the 14-day trial (not Free Edition) for classic compute
#
# Resources created: 2–4 (depending on optional features)
# -----------------------------------------------------------------------------

# ═════════════════════════════════════════════════════════════════════════════
# 1. DATA SOURCES
# ═════════════════════════════════════════════════════════════════════════════
# These data sources dynamically resolve the best Spark runtime and node type
# for the workspace. This avoids hardcoding version strings that become stale.

# Current user — needed for single-user cluster mode (Unity Catalog requirement
# for single-node clusters).
data "databricks_current_user" "me" {}

# Latest LTS Spark runtime — Long Term Support versions are the most stable
# and recommended for production-like workloads.
data "databricks_spark_version" "lts" {
  long_term_support = true
}

# Smallest available node type — minimizes cost for the PoC. The provider
# selects the cheapest instance type with at least the specified resources.
data "databricks_node_type" "smallest" {
  local_disk = true
}

# ═════════════════════════════════════════════════════════════════════════════
# 2. CLUSTER POLICY
# ═════════════════════════════════════════════════════════════════════════════
# Enforces PoC cost controls. Even though there's typically one user, the
# policy prevents accidental large cluster creation and ensures auto-terminate.

resource "databricks_cluster_policy" "poc" {
  name = "security-lakehouse-poc"

  definition = jsonencode({
    # Restrict to small instance types — prevents accidentally spinning up
    # expensive compute. The "allowlist" type constrains the dropdown in the UI.
    "node_type_id" : {
      "type" : "allowlist",
      "values" : [
        data.databricks_node_type.smallest.id,
      ],
      "defaultValue" : data.databricks_node_type.smallest.id
    },

    # Single-node only — max 0 workers for PoC to minimize cost.
    "num_workers" : {
      "type" : "fixed",
      "value" : 0
    },

    # Auto-terminate after inactivity — critical cost control.
    "autotermination_minutes" : {
      "type" : "range",
      "minValue" : 10,
      "maxValue" : 60,
      "defaultValue" : var.auto_termination_minutes
    },

    # Unity Catalog requires a data security mode. SINGLE_USER is the only
    # option for single-node clusters.
    "data_security_mode" : {
      "type" : "fixed",
      "value" : "SINGLE_USER"
    }
  })
}

# ═════════════════════════════════════════════════════════════════════════════
# 3. CLUSTER
# ═════════════════════════════════════════════════════════════════════════════
# Single-node cluster for running Auto Loader notebooks and ad-hoc queries.
# Unity Catalog is enabled via data_security_mode = SINGLE_USER, which
# restricts the cluster to one user but allows full UC access.
#
# Conditional: set enable_cluster = false if Free Edition (no classic compute).

resource "databricks_cluster" "poc" {
  count = var.enable_cluster ? 1 : 0

  cluster_name            = "security-lakehouse-poc"
  spark_version           = data.databricks_spark_version.lts.id
  node_type_id            = data.databricks_node_type.smallest.id
  policy_id               = databricks_cluster_policy.poc.id
  autotermination_minutes = var.auto_termination_minutes
  num_workers             = 0

  # Unity Catalog integration — SINGLE_USER mode is required for single-node
  # clusters. The current user is set as the single user.
  data_security_mode = "SINGLE_USER"
  single_user_name   = data.databricks_current_user.me.user_name

  # Set the default catalog so notebooks don't need explicit USE CATALOG.
  spark_conf = {
    "spark.databricks.sql.initial.catalog.name" = var.catalog_name
  }

  # Single-node cluster configuration — Spark runs in local mode.
  custom_tags = {
    "ResourceClass" = "SingleNode"
    "Project"       = "security-lakehouse"
    "Environment"   = "poc"
  }
}

# ═════════════════════════════════════════════════════════════════════════════
# 4. SQL WAREHOUSE (optional)
# ═════════════════════════════════════════════════════════════════════════════
# Serverless SQL warehouse for ad-hoc queries and dashboards. Only created
# if the free trial supports serverless SQL (many free trials do not).

resource "databricks_sql_endpoint" "poc" {
  count = var.enable_sql_warehouse ? 1 : 0

  name             = "security-lakehouse-poc"
  cluster_size     = "2X-Small"
  max_num_clusters = 1

  # Auto-stop after 15 minutes of inactivity — serverless warehouses have
  # per-second billing so auto-stop is critical for cost control.
  auto_stop_mins = 15

  # CHANNEL_NAME_CURRENT gets the latest SQL runtime with bug fixes.
  channel {
    name = "CHANNEL_NAME_CURRENT"
  }

  tags {
    custom_tags {
      key   = "Project"
      value = "security-lakehouse"
    }
    custom_tags {
      key   = "Environment"
      value = "poc"
    }
  }
}

# ═════════════════════════════════════════════════════════════════════════════
# 5. GIT REPO (optional)
# ═════════════════════════════════════════════════════════════════════════════
# Connects a git repository to the workspace so notebooks and code are
# version-controlled. Only created when a git_repo_url is provided.

resource "databricks_repo" "this" {
  count = var.git_repo_url != "" ? 1 : 0

  url          = var.git_repo_url
  git_provider = var.git_provider

  # Repo syncs to the workspace's Repos directory. The path is auto-derived
  # from the repository name by Databricks.
}
