# -----------------------------------------------------------------------------
# SNS Alerts Module
# -----------------------------------------------------------------------------
# Creates the AWS-side infrastructure for forwarding gold.alerts rows out of
# Databricks. A Databricks notebook (03_gold_alerts_forward) publishes one
# JSON message per unforwarded alert to this SNS topic; downstream integrations
# (email subscriptions, SQS queues, Lambda functions, SIEM connectors) are
# attached to the topic independently and are not managed here.
#
# Resources created: 5
#   1. SNS topic
#   2. SNS topic policy  (restricts Publish to the publisher IAM user)
#   3. IAM user          (lakehouse-sns-publisher — least privilege)
#   4. IAM user policy   (sns:Publish on this topic only)
#   5. IAM access key    (programmatic credentials stored in Terraform state)
#
# Security note — IAM access key in Terraform state:
#   The access key secret is stored in the Terraform state file. This is
#   acceptable for a PoC where state is in a private S3 bucket with versioning
#   and SSE enabled. For production, replace the IAM user + access key with an
#   IAM role that Databricks can assume via STS, eliminating long-lived
#   credentials entirely.
#
# Prerequisites:
#   - Security account AWS provider configured in the root module
#   - Phase 2 applied (SNS lives in the same security account)
# -----------------------------------------------------------------------------

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  module_tags = merge(var.tags, {
    Component = "sns-alerts"
  })
}

# ═════════════════════════════════════════════════════════════════════════════
# 1. SNS TOPIC
# ═════════════════════════════════════════════════════════════════════════════
# Standard (not FIFO) topic — alert ordering is not required. Downstream
# subscribers (email, SQS, Lambda) are configured out of band.

resource "aws_sns_topic" "alerts" {
  name = var.topic_name

  tags = merge(local.module_tags, {
    Name = var.topic_name
  })
}

# ═════════════════════════════════════════════════════════════════════════════
# 2. SNS TOPIC POLICY
# ═════════════════════════════════════════════════════════════════════════════
# Restricts who may publish to the topic. Only the dedicated publisher IAM
# user is granted Publish. The topic owner (the security account) retains
# full administrative access so subscriptions and policy updates can be made
# via the console or future Terraform changes without this policy blocking them.

data "aws_iam_policy_document" "sns_topic_policy" {
  # Grant the dedicated publisher IAM user the ability to publish to this topic.
  # The account root does not need an explicit Allow here — AWS account owners
  # always retain implicit administrative access to resources in their own account,
  # so omitting the root statement does not cause a lockout. SNS rejects sns:*
  # wildcards in topic policies (out-of-service-scope error), so listing only the
  # specific action needed is both required and least-privilege.
  statement {
    sid    = "DatabricksPublisherOnly"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = [aws_iam_user.sns_publisher.arn]
    }

    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.sns_topic_policy.json

  # The policy references the publisher user ARN, so the user must exist first.
  depends_on = [aws_iam_user.sns_publisher]
}

# ═════════════════════════════════════════════════════════════════════════════
# 3. IAM USER — sns publisher
# ═════════════════════════════════════════════════════════════════════════════
# Dedicated IAM user with the narrowest possible permission set. This user's
# sole purpose is allowing the Databricks notebook to call sns:Publish.
# It has no console access, no MFA, and no other AWS permissions.

resource "aws_iam_user" "sns_publisher" {
  name = var.publisher_iam_user_name
  path = "/databricks/"

  tags = merge(local.module_tags, {
    Name    = var.publisher_iam_user_name
    Purpose = "Databricks gold-layer SNS alert forwarding"
  })
}

# ═════════════════════════════════════════════════════════════════════════════
# 4. IAM USER POLICY
# ═════════════════════════════════════════════════════════════════════════════
# Inline policy granting exactly one action on exactly one resource.
# Scoped to this topic ARN — the user cannot publish to any other topic.

data "aws_iam_policy_document" "sns_publish" {
  statement {
    sid    = "PublishToAlertsTopicOnly"
    effect = "Allow"

    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_iam_user_policy" "sns_publish" {
  name   = "sns-publish-alerts-topic"
  user   = aws_iam_user.sns_publisher.name
  policy = data.aws_iam_policy_document.sns_publish.json
}

# ═════════════════════════════════════════════════════════════════════════════
# 5. IAM ACCESS KEY
# ═════════════════════════════════════════════════════════════════════════════
# Programmatic access key for the publisher user. The secret is stored in
# Terraform state and passed directly into Databricks Secrets (see the
# databricks/jobs module) so the notebook can retrieve it via dbutils.secrets.
#
# Rotation: rotate by tainting this resource and re-applying. The new values
# automatically flow into Databricks Secrets on the same apply.

resource "aws_iam_access_key" "sns_publisher" {
  user = aws_iam_user.sns_publisher.name
}
