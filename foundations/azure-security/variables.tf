variable "subscription_id" {
  description = "Azure subscription ID."
  type        = string
}

variable "location" {
  description = "Azure region."
  type        = string
  default     = "eastus"
}

variable "name_prefix" {
  description = "Name prefix for all resources."
  type        = string
  default     = "lakehouse"
}
