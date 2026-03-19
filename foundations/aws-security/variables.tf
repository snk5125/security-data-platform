variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "security_account_id" {
  description = "AWS account ID of the security/management account."
  type        = string
}

variable "organization_id" {
  description = "AWS Organizations ID."
  type        = string
}

variable "managed_storage_bucket_name" {
  description = "Name for the managed storage S3 bucket."
  type        = string
}
