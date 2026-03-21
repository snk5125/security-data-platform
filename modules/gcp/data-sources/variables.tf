variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "GCP region."
  type        = string
  default     = "us-central1"
}

variable "name_prefix" {
  description = "Name prefix for all resources."
  type        = string
}

variable "service_account_email" {
  description = "Email of the Databricks service account (from gcp-security foundation). Granted read access on logs bucket."
  type        = string
}

variable "network_name" {
  description = "VPC network name (from baseline module). Used to scope VPC Flow Log sink filter."
  type        = string
}

variable "subnet_name" {
  description = "Subnet name (from baseline module). Used for log sink filter."
  type        = string
}

variable "enable_scc" {
  description = "Enable SCC Findings export. Requires org-level SCC Standard activation. Default: false."
  type        = bool
  default     = false
}
