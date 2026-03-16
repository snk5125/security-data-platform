# -----------------------------------------------------------------------------
# Input Variables — Workload Account Baseline Module
# -----------------------------------------------------------------------------
# Parameterizes the VPC, networking, and EC2 instances deployed into each
# workload account. Each invocation targets a different provider alias.
# -----------------------------------------------------------------------------

variable "account_alias" {
  description = "Short name for this workload account (e.g., 'workload-a'). Used in resource naming."
  type        = string
}

variable "account_id" {
  description = "AWS account ID of the workload account — used in resource naming and tagging"
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the workload VPC (e.g., 10.0.0.0/16)"
  type        = string
}

variable "public_subnet_cidr" {
  description = "CIDR block for the single public subnet (must be within vpc_cidr)"
  type        = string
}

variable "availability_zone" {
  description = "AZ for the public subnet (e.g., us-east-1a)"
  type        = string
  default     = "us-east-1a"
}

variable "instance_type" {
  description = "EC2 instance type for both Linux and Windows instances"
  type        = string
  default     = "t2.micro"
}

variable "tags" {
  description = "Additional tags to merge with module-level defaults"
  type        = map(string)
  default     = {}
}
