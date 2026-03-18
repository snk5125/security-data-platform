# -----------------------------------------------------------------------------
# Input Variables — SNS Alerts Module
# -----------------------------------------------------------------------------

variable "topic_name" {
  description = "SNS topic name — receives gold.alerts forwarded from Databricks"
  type        = string
  default     = "security-lakehouse-alerts"
}

variable "publisher_iam_user_name" {
  description = "IAM user name for the Databricks SNS publisher (least-privilege, sns:Publish only)"
  type        = string
  default     = "lakehouse-sns-publisher"
}

variable "tags" {
  description = "Tags applied to all resources in this module"
  type        = map(string)
  default     = {}
}
