# -----------------------------------------------------------------------------
# Azure Security Foundation Module
# -----------------------------------------------------------------------------
# Creates the hub-level Azure resources for Databricks integration:
#   1. Resource Group for security hub resources
#   2. ADLS Gen2 Storage Account (hierarchical namespace) for Databricks
#      managed storage — Azure counterpart to the AWS managed S3 bucket
#   3. Entra ID (Azure AD) App Registration + Service Principal + Client Secret
#      for Databricks to authenticate to ADLS Gen2
#   4. Role assignment: SP gets Storage Blob Data Contributor on managed storage
#
# The service principal credentials are passed to the Databricks
# databricks_storage_credential resource in the hub root's cloud-integration
# module. Databricks uses them to access ADLS via the azure_service_principal
# credential type — no AWS IAM involved.
#
# Prerequisites:
#   - Azure CLI authenticated (`az login`)
#   - Subscription exists and is accessible
#   - Entra ID permissions to create App Registrations (default for users)
# -----------------------------------------------------------------------------

locals {
  module_tags = merge(var.tags, {
    Component = "azure-security-foundation"
    ManagedBy = "terraform"
  })
}

# ═════════════════════════════════════════════════════════════════════════════
# 1. RESOURCE GROUP
# ═════════════════════════════════════════════════════════════════════════════

resource "azurerm_resource_group" "security_hub" {
  name     = "${var.name_prefix}-rg-security-hub"
  location = var.location
  tags     = local.module_tags
}

# ═════════════════════════════════════════════════════════════════════════════
# 2. ADLS GEN2 STORAGE ACCOUNT — Managed Storage
# ═════════════════════════════════════════════════════════════════════════════
# Hierarchical namespace (HNS) enabled for ADLS Gen2 compatibility.
# This account is the Azure-side managed storage for Databricks Unity Catalog.
# The service principal needs Storage Blob Data Contributor for read/write.

resource "azurerm_storage_account" "managed" {
  name                     = "${replace(var.name_prefix, "-", "")}managed"
  resource_group_name      = azurerm_resource_group.security_hub.name
  location                 = azurerm_resource_group.security_hub.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  account_kind             = "StorageV2"
  is_hns_enabled           = true # Required for ADLS Gen2

  tags = local.module_tags
}

# ═════════════════════════════════════════════════════════════════════════════
# 3. ENTRA ID APP REGISTRATION + SERVICE PRINCIPAL
# ═════════════════════════════════════════════════════════════════════════════
# Creates an application in Entra ID (Azure AD) with a client secret.
# Databricks uses these credentials in its azure_service_principal storage
# credential block to access ADLS Gen2 storage.
#
# The azuread provider is required because azurerm cannot create Entra ID
# application registrations — they live in the directory, not in a subscription.

data "azuread_client_config" "current" {}

resource "azuread_application" "databricks" {
  display_name = "${var.name_prefix}-databricks-sp"
  owners       = [data.azuread_client_config.current.object_id]
}

resource "azuread_service_principal" "databricks" {
  client_id = azuread_application.databricks.client_id
  owners    = [data.azuread_client_config.current.object_id]
}

resource "azuread_application_password" "databricks" {
  application_id = azuread_application.databricks.id
  display_name   = "${var.name_prefix}-databricks-secret"
  end_date       = "2027-12-31T00:00:00Z"
}

# ═════════════════════════════════════════════════════════════════════════════
# 4. ROLE ASSIGNMENT — SP → Storage Blob Data Contributor on managed storage
# ═════════════════════════════════════════════════════════════════════════════
# Databricks needs read/write access to the managed storage account for
# managed Delta tables. The Contributor role on the storage account scope
# grants this access via the service principal.

resource "azurerm_role_assignment" "sp_managed_storage" {
  scope                = azurerm_storage_account.managed.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azuread_service_principal.databricks.object_id
}
