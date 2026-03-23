# -----------------------------------------------------------------------------
# Azure Data Sources Module
# -----------------------------------------------------------------------------
# Creates security log collection infrastructure for an Azure workload:
#   1. ADLS Gen2 Storage Account for security logs (separate from managed storage)
#   2. Storage containers per data source (activitylog, vnet-flow-logs)
#   3. Activity Log Diagnostic Setting → ADLS container
#   4. VNet Flow Logs → ADLS container
#   5. (Toggle) Defender for Cloud Servers plan
#   6. (Toggle) Resource Graph daily export via Logic App
#   7. Role assignment: SP gets Storage Blob Data Reader on logs storage
#
# Mirrors the AWS data-sources module pattern. The service principal from the
# security foundation module gets read-only access to the logs — Databricks
# reads these via the Azure storage credential.
#
# Prerequisites:
#   - Workload baseline applied (VNet and NSG exist)
#   - Security foundation applied (service principal exists)
#   - Network Watcher auto-created in the region (Azure default)
# -----------------------------------------------------------------------------

locals {
  module_tags = merge(var.tags, {
    Component = "data-sources"
    ManagedBy = "terraform"
  })
}

# ═════════════════════════════════════════════════════════════════════════════
# 1. ADLS GEN2 STORAGE ACCOUNT — Security Logs
# ═════════════════════════════════════════════════════════════════════════════
# Separate from managed storage. Holds raw security logs exported by
# Azure services. The Databricks service principal gets read-only access.

resource "azurerm_storage_account" "security_logs" {
  # Storage account names: 3-24 chars, lowercase alphanumeric only.
  # Truncate the prefix to 20 chars to leave room for "logs" suffix.
  name                     = "${substr(replace(var.name_prefix, "-", ""), 0, 20)}logs"
  resource_group_name      = var.resource_group_name
  location                 = var.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  account_kind             = "StorageV2"
  is_hns_enabled           = true # Required for ADLS Gen2

  tags = local.module_tags
}

# ═════════════════════════════════════════════════════════════════════════════
# 1b. STANDARD STORAGE ACCOUNT — VNet Flow Logs
# ═════════════════════════════════════════════════════════════════════════════
# Network Watcher flow logs require a standard (non-HNS) storage account —
# they use the Blob API which is incompatible with ADLS Gen2 hierarchical
# namespace. This account is separate from the ADLS Gen2 account used for
# Activity Log and other diagnostic data.

resource "azurerm_storage_account" "flow_logs" {
  name                     = "${substr(replace(var.name_prefix, "-", ""), 0, 20)}flow"
  resource_group_name      = var.resource_group_name
  location                 = var.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  account_kind             = "StorageV2"
  # is_hns_enabled deliberately omitted (defaults to false)

  tags = local.module_tags
}

# NOTE: Azure diagnostic settings and VNet flow logs write to system-managed
# containers (insights-activity-logs/ and insights-logs-flowlogflowevent/)
# at the storage account level. No explicitly-created containers are needed
# for these data sources. The Databricks external locations in the
# cloud-integration module point directly to these system-managed containers.

# ═════════════════════════════════════════════════════════════════════════════
# 2. ROLE ASSIGNMENT — SP → Storage Blob Data Reader on logs storage
# ═════════════════════════════════════════════════════════════════════════════
# The Entra ID service principal (from security foundation) gets read-only
# access. Databricks reads logs via the Azure storage credential but never
# writes to them — security logs are written by Azure services.

resource "azurerm_role_assignment" "sp_logs_reader" {
  scope                = azurerm_storage_account.security_logs.id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = var.service_principal_id
}

# Grant the service principal read access to the flow logs storage account
# so Databricks can read VNet Flow Log data via the Azure storage credential.
resource "azurerm_role_assignment" "sp_flow_logs_reader" {
  scope                = azurerm_storage_account.flow_logs.id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = var.service_principal_id
}

# ═════════════════════════════════════════════════════════════════════════════
# 3. ACTIVITY LOG — Diagnostic Setting → ADLS
# ═════════════════════════════════════════════════════════════════════════════
# Exports subscription-level Activity Log events to the storage account.
# Azure writes to a system-managed path structure under the storage account:
#   insights-activity-logs/resourceId=/SUBSCRIPTIONS/{sub}/y={year}/m={month}/d={day}/h={hour}/m=00/
# This is NOT the activitylog/ container — Azure diagnostic settings manage
# their own path layout. The bronze notebook must use the storage account URL
# (not a container-scoped path) and read from insights-activity-logs/.
#
# Scope is the subscription (not resource group) because Activity Log is a
# subscription-level resource.

resource "azurerm_monitor_diagnostic_setting" "activity_log" {
  name               = "${var.name_prefix}-activity-log-export"
  target_resource_id = "/subscriptions/${var.subscription_id}"
  storage_account_id = azurerm_storage_account.security_logs.id

  enabled_log {
    category = "Administrative"
  }

  enabled_log {
    category = "Security"
  }

  enabled_log {
    category = "Alert"
  }

  enabled_log {
    category = "Policy"
  }
}

# ═════════════════════════════════════════════════════════════════════════════
# 4. VNET FLOW LOGS → Storage Account
# ═════════════════════════════════════════════════════════════════════════════
# VNet-level flow logs (azurerm v4+ required — uses target_resource_id).
# Azure writes to the system-managed insights-logs-flowlogflowevent container
# on the security_logs ADLS Gen2 storage account. Databricks reads this via
# abfss:// which requires HNS. The Databricks external location for this
# container is created in the cloud-integration module.
#
# Network Watcher is managed explicitly in the same resource group.

resource "azurerm_resource_group" "network_watcher" {
  name     = "NetworkWatcherRG"
  location = var.location
  tags     = local.module_tags
}

resource "azurerm_network_watcher" "main" {
  name                = "NetworkWatcher_${var.location}"
  location            = var.location
  resource_group_name = azurerm_resource_group.network_watcher.name
  tags                = local.module_tags
}

resource "azurerm_network_watcher_flow_log" "vnet" {
  name                 = "${var.name_prefix}-vnet-flow-log"
  network_watcher_name = azurerm_network_watcher.main.name
  resource_group_name  = azurerm_resource_group.network_watcher.name
  target_resource_id   = var.vnet_id
  storage_account_id   = azurerm_storage_account.security_logs.id
  enabled              = true
  version              = 2

  retention_policy {
    enabled = true
    days    = 30
  }

  tags = local.module_tags
}

# ═════════════════════════════════════════════════════════════════════════════
# 5. DEFENDER FOR CLOUD (toggle, default off)
# ═════════════════════════════════════════════════════════════════════════════
# Activates the Servers plan (~$15/server/month). Only created when
# enable_defender = true. Free tier is the default.

resource "azurerm_security_center_subscription_pricing" "servers" {
  count         = var.enable_defender ? 1 : 0
  tier          = "Standard"
  resource_type = "VirtualMachines"
}

# ═════════════════════════════════════════════════════════════════════════════
# 6. RESOURCE GRAPH EXPORT (toggle, default off)
# ═════════════════════════════════════════════════════════════════════════════
# Placeholder for Logic App that queries Resource Graph daily and writes
# JSON to ADLS. Deferred — enable_resource_graph creates the container only.

resource "azurerm_storage_container" "resource_graph" {
  count              = var.enable_resource_graph ? 1 : 0
  name               = "resource-graph"
  storage_account_id = azurerm_storage_account.security_logs.id
}
