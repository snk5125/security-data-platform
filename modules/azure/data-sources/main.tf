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

# ── Storage Containers ──
# NOTE: Activity Log diagnostic settings create their own path structure
# (insights-activity-logs/) at the storage account level — they do NOT use
# a container we create. VNet Flow Logs also write to system-managed paths.
# These containers are kept for organizational documentation only.

# Root container for Databricks external location. The abfss:// protocol
# requires a container name — this provides the anchor for the external
# location URL while actual data lives in service-managed containers below.
resource "azurerm_storage_container" "security_logs" {
  name                  = "security-logs"
  storage_account_name  = azurerm_storage_account.security_logs.name
  container_access_type = "private"
}

resource "azurerm_storage_container" "vnet_flow_logs" {
  name                  = "vnet-flow-logs"
  storage_account_name  = azurerm_storage_account.security_logs.name
  container_access_type = "private"
}

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
# 4. VNET FLOW LOGS
# ═════════════════════════════════════════════════════════════════════════════
# VNet Flow Logs — SKIPPED for azurerm v3.x.
# Azure blocked new NSG flow log creation on June 30, 2025. VNet-level
# flow logs require azurerm v4.x (uses target_resource_id instead of
# network_security_group_id). When upgrading to azurerm ~> 4.0, re-enable
# the flow log resource below with target_resource_id = var.vnet_id.
#
# For now, the vnet-flow-logs/ container exists but receives no data.
# The bronze VNet Flow Log notebook will simply skip with "no files found."

# Network Watcher — managed explicitly so it's available when flow logs
# are re-enabled after provider upgrade.
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

# TODO: Re-enable after upgrading to azurerm ~> 4.0
# resource "azurerm_network_watcher_flow_log" "vnet" {
#   name                 = "${var.name_prefix}-vnet-flow-log"
#   network_watcher_name = azurerm_network_watcher.main.name
#   resource_group_name  = azurerm_resource_group.network_watcher.name
#   target_resource_id   = var.vnet_id
#   storage_account_id   = azurerm_storage_account.security_logs.id
#   enabled              = true
#   version              = 2
#   retention_policy {
#     enabled = true
#     days    = 30
#   }
#   tags = local.module_tags
# }

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
  count                 = var.enable_resource_graph ? 1 : 0
  name                  = "resource-graph"
  storage_account_name  = azurerm_storage_account.security_logs.name
  container_access_type = "private"
}
