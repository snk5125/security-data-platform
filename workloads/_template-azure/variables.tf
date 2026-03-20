variable "subscription_id" {
  description = "Azure subscription ID."
  type        = string
}

variable "location" {
  description = "Azure region."
  type        = string
  default     = "eastus"
}

variable "workload_alias" {
  description = "Short alias for this workload (e.g., 'workload-a'). Used in resource names and manifest output."
  type        = string
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
  default     = "lakehouse"
}

variable "service_principal_id" {
  description = "Object ID of the Entra ID service principal (from azure-security foundation). Granted read access on logs storage."
  type        = string
}
