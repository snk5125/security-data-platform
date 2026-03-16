# -----------------------------------------------------------------------------
# Outputs — Workspace Configuration Module
# -----------------------------------------------------------------------------
# Exposes resource identifiers for downstream phases (Auto Loader notebooks,
# job definitions, validation scripts).
# -----------------------------------------------------------------------------

output "cluster_policy_id" {
  description = "Cluster policy ID — used to verify policy enforcement"
  value       = databricks_cluster_policy.poc.id
}

output "cluster_id" {
  description = "Cluster ID — used by Phase 8 Auto Loader notebooks and jobs"
  value       = var.enable_cluster ? databricks_cluster.poc[0].id : null
}

output "cluster_name" {
  description = "Cluster name — human-readable identifier for the PoC cluster"
  value       = var.enable_cluster ? databricks_cluster.poc[0].cluster_name : null
}

output "sql_warehouse_id" {
  description = "SQL warehouse ID — used for ad-hoc queries if serverless is enabled"
  value       = var.enable_sql_warehouse ? databricks_sql_endpoint.poc[0].id : null
}

output "git_repo_id" {
  description = "Git repo ID in the workspace — null if no repo URL was provided"
  value       = var.git_repo_url != "" ? databricks_repo.this[0].id : null
}
