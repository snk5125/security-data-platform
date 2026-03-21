variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "GCP region for all resources."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP zone for compute instances."
  type        = string
  default     = "us-central1-a"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC subnet (GCP VPCs don't have CIDRs; this is the subnet CIDR)."
  type        = string
}

variable "name_prefix" {
  description = "Name prefix for all resources."
  type        = string
}
