variable "resource_group_name" {
  description = "Name of the resource group for this workload."
  type        = string
}

variable "location" {
  description = "Azure region for all resources."
  type        = string
  default     = "eastus"
}

variable "vnet_cidr" {
  description = "CIDR block for the workload VNet."
  type        = string
}

variable "subnet_cidr" {
  description = "CIDR block for the public subnet (must be within vnet_cidr)."
  type        = string
}

variable "name_prefix" {
  description = "Name prefix for all resources."
  type        = string
}

variable "admin_username" {
  description = "Admin username for VMs."
  type        = string
  default     = "azureadmin"
}

variable "windows_admin_password" {
  description = "Admin password for Windows VM. Demo only — not for production use."
  type        = string
  default     = "P@ssw0rd1234!"
  sensitive   = true
}

variable "tags" {
  description = "Tags to apply to all resources."
  type        = map(string)
  default     = {}
}
