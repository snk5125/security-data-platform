variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "GCP region."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP zone for compute instances."
  type        = string
  default     = "us-central1-a"
}

variable "workload_alias" {
  description = "Short alias for this workload (e.g., 'gcp-workload-a'). Used in resource names and manifest output."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the workload subnet."
  type        = string
}

variable "name_prefix" {
  description = "Name prefix for all resources."
  type        = string
  default     = "lakehouse"
}

variable "service_account_email" {
  description = "Email of the Databricks service account (from gcp-security foundation). Granted read access on logs storage."
  type        = string
}

variable "enable_scc" {
  description = "Enable SCC Findings export. Default: false."
  type        = bool
  default     = false
}
