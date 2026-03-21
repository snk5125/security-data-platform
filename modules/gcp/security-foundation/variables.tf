variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "GCP region for all resources."
  type        = string
  default     = "us-central1"
}

variable "name_prefix" {
  description = "Name prefix for all resources (e.g., 'lakehouse')."
  type        = string
  default     = "lakehouse"
}
