variable "resource_group_name" {
  description = "Resource group for data source resources."
  type        = string
}

variable "location" {
  description = "Azure region."
  type        = string
  default     = "eastus"
}

variable "subscription_id" {
  description = "Azure subscription ID — used for Activity Log diagnostic setting scope."
  type        = string
}

variable "vnet_id" {
  description = "VNet ID for VNet Flow Logs."
  type        = string
}

variable "nsg_id" {
  description = "NSG ID for VNet Flow Logs (fallback if VNet flow logs unavailable)."
  type        = string
}

variable "name_prefix" {
  description = "Name prefix for all resources."
  type        = string
}

variable "service_principal_id" {
  description = "Object ID of the Entra ID service principal — granted Storage Blob Data Reader on logs storage."
  type        = string
}

variable "enable_defender" {
  description = "Enable Defender for Cloud Servers plan (~$15/server/month). Default: false (free tier)."
  type        = bool
  default     = false
}

variable "enable_resource_graph" {
  description = "Enable Resource Graph daily export via Logic App. Default: false."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags to apply to all resources."
  type        = map(string)
  default     = {}
}
