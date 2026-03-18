# -----------------------------------------------------------------------------
# Provider Requirements — SNS Alerts Module
# -----------------------------------------------------------------------------
# Uses only the AWS provider. The root module passes the default (security
# account) provider — SNS topic and IAM user both live in the security account.
# -----------------------------------------------------------------------------

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.50"
    }
  }
}
