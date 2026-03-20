variable "subscription_id" {
  description = "Azure subscription ID."
  type        = string
}

variable "location" {
  description = "Azure region for all resources."
  type        = string
  default     = "eastus"
}

variable "name_prefix" {
  description = "Name prefix for all resources (e.g., 'lakehouse')."
  type        = string
  default     = "lakehouse"
}

variable "tags" {
  description = "Tags to apply to all resources."
  type        = map(string)
  default     = {}
}
