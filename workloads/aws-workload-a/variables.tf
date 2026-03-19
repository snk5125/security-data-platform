variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "account_alias" {
  description = "Short alias for this workload account (e.g., 'workload-a')."
  type        = string
}

variable "account_id" {
  description = "AWS account ID for this workload account."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the workload VPC."
  type        = string
}

variable "public_subnet_cidr" {
  description = "CIDR block for the public subnet (must be within vpc_cidr)."
  type        = string
}

variable "security_account_id" {
  description = "Security account ID — used to construct the deterministic hub role ARN."
  type        = string
}

variable "hub_role_name" {
  description = "Hub IAM role name. Must match the role created by the hub root."
  type        = string
  default     = "lakehouse-hub-role"
}
