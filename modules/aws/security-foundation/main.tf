# =============================================================================
# Security Foundation Module
# =============================================================================
# Creates the central infrastructure in the security account:
#   1. Managed storage S3 bucket — where Databricks writes Delta tables
#   2. SNS topic + publisher IAM user — for alert forwarding from Databricks
#
# IAM roles (hub + managed-storage) are NOT created here — they live in the
# hub root because their trust policies require Databricks-assigned external IDs.
# The bucket policy pre-authorizes those roles using deterministic ARNs.
# =============================================================================

locals {
  # Deterministic ARNs — roles created by the hub root. ARN-based bucket
  # policies don't require the principal to exist at policy creation time.
  managed_storage_role_arn = "arn:aws:iam::${var.security_account_id}:role/${var.managed_storage_role_name}"
  hub_role_arn             = "arn:aws:iam::${var.security_account_id}:role/${var.hub_role_name}"

  module_tags = merge(var.tags, {
    Component = "security-foundation"
  })
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ═══════════════════════════════════════════════════════════════════════════════
# 1. MANAGED STORAGE S3 BUCKET
# ═══════════════════════════════════════════════════════════════════════════════

resource "aws_s3_bucket" "managed_storage" {
  bucket = var.managed_storage_bucket_name

  tags = merge(local.module_tags, {
    Name = var.managed_storage_bucket_name
  })
}

resource "aws_s3_bucket_versioning" "managed_storage" {
  bucket = aws_s3_bucket.managed_storage.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "managed_storage" {
  bucket = aws_s3_bucket.managed_storage.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "managed_storage" {
  bucket = aws_s3_bucket.managed_storage.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Bucket policy: grants Databricks UC master role and the managed storage role
# read/write access. Uses deterministic ARNs for roles that don't exist yet.
data "aws_iam_policy_document" "managed_storage_bucket_policy" {
  statement {
    sid    = "DatabricksReadWrite"
    effect = "Allow"

    principals {
      type = "AWS"
      identifiers = [
        var.databricks_uc_master_role_arn,
        local.managed_storage_role_arn,
      ]
    }

    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]

    resources = [
      aws_s3_bucket.managed_storage.arn,
      "${aws_s3_bucket.managed_storage.arn}/*",
    ]
  }
}

resource "aws_s3_bucket_policy" "managed_storage" {
  bucket = aws_s3_bucket.managed_storage.id
  policy = data.aws_iam_policy_document.managed_storage_bucket_policy.json
}

# ═══════════════════════════════════════════════════════════════════════════════
# 2. SNS ALERT TOPIC
# ═══════════════════════════════════════════════════════════════════════════════

resource "aws_sns_topic" "alerts" {
  name = var.sns_topic_name

  tags = merge(local.module_tags, {
    Name = var.sns_topic_name
  })
}

# Topic policy: restricts Publish to the dedicated publisher IAM user.
data "aws_iam_policy_document" "sns_topic_policy" {
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

  depends_on = [aws_iam_user.sns_publisher]
}

# ═══════════════════════════════════════════════════════════════════════════════
# 3. SNS PUBLISHER IAM USER
# ═══════════════════════════════════════════════════════════════════════════════

resource "aws_iam_user" "sns_publisher" {
  name = var.sns_publisher_iam_user_name
  path = "/databricks/"

  tags = merge(local.module_tags, {
    Name    = var.sns_publisher_iam_user_name
    Purpose = "Databricks gold-layer SNS alert forwarding"
  })
}

data "aws_iam_policy_document" "sns_publish" {
  statement {
    sid       = "PublishToAlertsTopicOnly"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_iam_user_policy" "sns_publish" {
  name   = "sns-publish-alerts-topic"
  user   = aws_iam_user.sns_publisher.name
  policy = data.aws_iam_policy_document.sns_publish.json
}

resource "aws_iam_access_key" "sns_publisher" {
  user = aws_iam_user.sns_publisher.name
}
