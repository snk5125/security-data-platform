# -----------------------------------------------------------------------------
# Data Sources Module — Main Resources
# -----------------------------------------------------------------------------
# Creates security data sources in a single workload account:
#   1. S3 bucket for security logs (shared by all four data sources)
#   2. KMS key for GuardDuty S3 export encryption
#   3. Read-only IAM role for Databricks access (trusts the hub role)
#   4. CloudTrail (management events)
#   5. VPC Flow Logs (attached to workload VPC)
#   6. GuardDuty (detector + S3 export)
#   7. AWS Config (recorder + delivery channel + recorder status)
#
# Critical dependency chain:
#   S3 bucket → bucket policy → CloudTrail, Config delivery channel, GuardDuty export
#   KMS key → GuardDuty export
#   Config IAM role → policy attachment → recorder → delivery channel → recorder status
#
# The bucket policy MUST exist before CloudTrail and Config are created because
# both services validate the policy at resource creation time. Without it:
#   - CloudTrail: InsufficientS3BucketPolicyException
#   - Config: InsufficientDeliveryPolicyException
# -----------------------------------------------------------------------------

locals {
  # Consistent naming prefix across all resources in this account.
  name_prefix = "lakehouse-${var.account_alias}"

  # Deterministic CloudTrail ARN — used in the bucket policy BEFORE the trail
  # resource exists. This avoids a circular dependency: the bucket policy needs
  # the trail ARN, but the trail needs the bucket policy to exist first.
  # We construct it from known components instead of referencing the resource.
  cloudtrail_arn = "arn:aws:cloudtrail:${var.region}:${var.account_id}:trail/${local.name_prefix}-trail"

  # Merge module-specific tags with caller-provided tags.
  module_tags = merge(var.tags, {
    Component = "data-sources"
    Account   = var.account_alias
  })
}

# =============================================================================
# 1. Security Logs S3 Bucket
# =============================================================================
# Single bucket with prefix-based separation for all four data sources:
#   /cloudtrail/   — CloudTrail management events
#   /vpc-flow-logs/ — VPC Flow Logs
#   /guardduty/    — GuardDuty findings export
#   /config/       — AWS Config snapshots and history

resource "aws_s3_bucket" "security_logs" {
  bucket = "${local.name_prefix}-security-logs-${var.account_id}"

  tags = merge(local.module_tags, {
    Name = "${local.name_prefix}-security-logs"
  })
}

# Enable versioning for recovery support and compliance.
resource "aws_s3_bucket_versioning" "security_logs" {
  bucket = aws_s3_bucket.security_logs.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Server-side encryption with AES256 (S3-managed keys). GuardDuty export uses
# its own KMS key; the bucket default encryption covers CloudTrail/FlowLogs/Config.
resource "aws_s3_bucket_server_side_encryption_configuration" "security_logs" {
  bucket = aws_s3_bucket.security_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Block all public access — security logs must never be publicly exposed.
resource "aws_s3_bucket_public_access_block" "security_logs" {
  bucket = aws_s3_bucket.security_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# =============================================================================
# 1b. Host Telemetry S3 Bucket (Cribl Edge)
# =============================================================================
# Dedicated bucket for host-level telemetry collected by Cribl Edge agents.
# Kept separate from security logs so Auto Loader streams and schemas stay
# independent — security logs use service-native formats (CloudTrail JSON,
# VPC Flow parquet, etc.) while host telemetry uses Cribl's own format.
#
# Count-gated: only created when var.enable_host_telemetry = true.
# No KMS key required — AES256 (S3-managed keys) is sufficient for this data.
#
# Access model:
#   - Hub role: cross-account read (GetObject, ListBucket, GetBucketLocation)
#     for Databricks Unity Catalog external location
#   - Cribl writer IAM user: write-only (PutObject, GetBucketLocation)
#     credentials are output as a sensitive value for Cribl Edge configuration

resource "aws_s3_bucket" "host_telemetry" {
  count  = var.enable_host_telemetry ? 1 : 0
  bucket = "${local.name_prefix}-host-telemetry-${var.account_id}"

  tags = merge(local.module_tags, {
    Name = "${local.name_prefix}-host-telemetry"
  })
}

resource "aws_s3_bucket_versioning" "host_telemetry" {
  count  = var.enable_host_telemetry ? 1 : 0
  bucket = aws_s3_bucket.host_telemetry[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

# Server-side encryption with AES256 (S3-managed keys).
resource "aws_s3_bucket_server_side_encryption_configuration" "host_telemetry" {
  count  = var.enable_host_telemetry ? 1 : 0
  bucket = aws_s3_bucket.host_telemetry[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Block all public access — host telemetry must never be publicly exposed.
resource "aws_s3_bucket_public_access_block" "host_telemetry" {
  count  = var.enable_host_telemetry ? 1 : 0
  bucket = aws_s3_bucket.host_telemetry[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# -----------------------------------------------------------------------------
# Host Telemetry Bucket Policy
# -----------------------------------------------------------------------------
# Two principals need access:
#   1. Hub role (cross-account read) — same pattern as security logs bucket
#   2. Cribl writer IAM user (write-only) — PutObject for agent uploads
#
# The bucket policy is the bucket-side authorization; the Cribl user's IAM
# policy is the identity-side authorization. Both must allow the operation.

data "aws_iam_policy_document" "host_telemetry_bucket_policy" {
  count = var.enable_host_telemetry ? 1 : 0

  # --- Hub Role: cross-account read access for Databricks Unity Catalog ---
  statement {
    sid    = "HubRoleCrossAccountRead"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = [var.hub_role_arn]
    }
    actions = [
      "s3:GetObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [
      aws_s3_bucket.host_telemetry[0].arn,
      "${aws_s3_bucket.host_telemetry[0].arn}/*",
    ]
  }

  # --- Cribl Writer: write access for agent telemetry uploads ---
  statement {
    sid    = "CriblWriterPutObject"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = [aws_iam_user.cribl_writer[0].arn]
    }
    actions = [
      "s3:PutObject",
      "s3:GetBucketLocation",
    ]
    resources = [
      aws_s3_bucket.host_telemetry[0].arn,
      "${aws_s3_bucket.host_telemetry[0].arn}/*",
    ]
  }
}

resource "aws_s3_bucket_policy" "host_telemetry" {
  count  = var.enable_host_telemetry ? 1 : 0
  bucket = aws_s3_bucket.host_telemetry[0].id
  policy = data.aws_iam_policy_document.host_telemetry_bucket_policy[0].json
}

# =============================================================================
# 1c. Cribl Writer IAM User
# =============================================================================
# Dedicated IAM user for Cribl Edge agents to write host telemetry to S3.
# Uses an inline policy (not managed) because this is a single-purpose user
# that should never share its policy with other identities.
#
# The access key ID and secret are exposed as sensitive outputs for Cribl
# Edge configuration. These credentials grant write-only access to the
# host telemetry bucket — no read, no delete, no other buckets.

resource "aws_iam_user" "cribl_writer" {
  count = var.enable_host_telemetry ? 1 : 0
  name  = "${local.name_prefix}-cribl-writer"

  tags = merge(local.module_tags, {
    Name = "${local.name_prefix}-cribl-writer"
  })
}

# Inline policy: PutObject + GetBucketLocation on the host telemetry bucket.
# GetBucketLocation is required by S3 clients to determine the correct
# regional endpoint for subsequent requests.
resource "aws_iam_user_policy" "cribl_writer" {
  count = var.enable_host_telemetry ? 1 : 0
  name  = "${local.name_prefix}-cribl-writer-s3-policy"
  user  = aws_iam_user.cribl_writer[0].name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "WriteHostTelemetry"
      Effect = "Allow"
      Action = [
        "s3:PutObject",
        "s3:GetBucketLocation",
        "s3:ListBucket",
      ]
      Resource = [
        aws_s3_bucket.host_telemetry[0].arn,
        "${aws_s3_bucket.host_telemetry[0].arn}/*",
      ]
    }]
  })
}

# Programmatic access key for the Cribl writer user. The secret is stored in
# Terraform state (encrypted at rest in S3 backend) and exposed as a sensitive
# output. Rotate by tainting this resource: terraform taint 'aws_iam_access_key.cribl_writer[0]'
resource "aws_iam_access_key" "cribl_writer" {
  count = var.enable_host_telemetry ? 1 : 0
  user  = aws_iam_user.cribl_writer[0].name
}

# -----------------------------------------------------------------------------
# Bucket Policy — grants four AWS service principals write access
# -----------------------------------------------------------------------------
# This is the most complex policy in the project. Each service has different
# requirements for principals, actions, conditions, and resource scoping.
# The policy must be applied BEFORE CloudTrail and Config resources are created.

data "aws_iam_policy_document" "security_logs_bucket_policy" {

  # --- CloudTrail: requires GetBucketAcl + PutObject with conditions ---
  # CloudTrail checks the bucket ACL before writing and requires the
  # SourceArn condition to prevent confused deputy attacks.
  statement {
    sid    = "CloudTrailAclCheck"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.security_logs.arn]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.cloudtrail_arn]
    }
  }

  statement {
    sid    = "CloudTrailWrite"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.security_logs.arn}/cloudtrail/AWSLogs/${var.account_id}/*"]
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.cloudtrail_arn]
    }
  }

  # --- VPC Flow Logs: delivery.logs.amazonaws.com ---
  # The delivery.logs.amazonaws.com service principal is used by VPC Flow Logs
  # when delivering to S3. It needs PutObject and GetBucketAcl.
  statement {
    sid    = "VPCFlowLogsWrite"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.security_logs.arn}/vpc-flow-logs/*"]
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }

  statement {
    sid    = "VPCFlowLogsAclCheck"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.security_logs.arn]
  }

  # --- GuardDuty: requires GetBucketLocation + PutObject ---
  # GuardDuty checks the bucket location before exporting findings.
  # When destination_arn is the bucket ARN, GuardDuty creates its own path
  # structure, so PutObject must cover the entire bucket (not just /guardduty/*).
  statement {
    sid    = "GuardDutyWrite"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["guardduty.amazonaws.com"]
    }
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.security_logs.arn}/*"]
  }

  statement {
    sid    = "GuardDutyGetBucketLocation"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["guardduty.amazonaws.com"]
    }
    actions   = ["s3:GetBucketLocation"]
    resources = [aws_s3_bucket.security_logs.arn]
  }

  # --- AWS Config: requires PutObject + GetBucketAcl + ListBucket ---
  # Config needs ListBucket to check for existing snapshots and GetBucketAcl
  # for permission validation at delivery channel creation time.
  statement {
    sid    = "ConfigWrite"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["config.amazonaws.com"]
    }
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.security_logs.arn}/config/*"]
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }

  statement {
    sid    = "ConfigBucketChecks"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["config.amazonaws.com"]
    }
    actions = [
      "s3:GetBucketAcl",
      "s3:ListBucket",
    ]
    resources = [aws_s3_bucket.security_logs.arn]
  }

  # --- Hub Role: cross-account read access for Databricks Unity Catalog ---
  # The hub role in the security account needs direct S3 read access to this
  # bucket. For cross-account S3 access, BOTH the caller's IAM policy AND
  # the bucket policy must allow the operation. The hub role's IAM policy
  # (Phase 2) grants s3:GetObject/ListBucket/GetBucketLocation, and this
  # statement is the bucket-side half of that cross-account authorization.
  # Without this, Databricks external location validation fails with
  # "AWS IAM role does not have READ permissions".
  statement {
    sid    = "HubRoleCrossAccountRead"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = [var.hub_role_arn]
    }
    actions = [
      "s3:GetObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [
      aws_s3_bucket.security_logs.arn,
      "${aws_s3_bucket.security_logs.arn}/*",
    ]
  }
}

resource "aws_s3_bucket_policy" "security_logs" {
  bucket = aws_s3_bucket.security_logs.id
  policy = data.aws_iam_policy_document.security_logs_bucket_policy.json
}

# =============================================================================
# 2. KMS Key for GuardDuty S3 Export
# =============================================================================
# GuardDuty S3 export MANDATES KMS encryption — you cannot create a publishing
# destination without a KMS key. The key policy must grant the GuardDuty service
# principal encrypt/decrypt permissions AND retain root account admin access.

data "aws_iam_policy_document" "guardduty_kms_policy" {
  # Root account retains full control — without this statement, you permanently
  # lose management access to the key. This is the most common KMS pitfall.
  statement {
    sid    = "RootAccountAdmin"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${var.account_id}:root"]
    }
    actions   = ["kms:*"]
    resources = ["*"]
  }

  # GuardDuty service can encrypt findings for S3 export.
  statement {
    sid    = "GuardDutyEncrypt"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["guardduty.amazonaws.com"]
    }
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    resources = ["*"]
  }

  # Hub role needs kms:Decrypt to read GuardDuty findings via Databricks.
  # GuardDuty exports are KMS-encrypted (unlike CloudTrail/VPC Flow/Config which
  # use SSE-S3). Without this, Databricks external location reads fail with
  # AccessDeniedException on kms:Decrypt.
  statement {
    sid    = "HubRoleDecrypt"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = [var.hub_role_arn]
    }
    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
    ]
    resources = ["*"]
  }
}

resource "aws_kms_key" "guardduty" {
  description             = "KMS key for GuardDuty findings export — ${local.name_prefix}"
  deletion_window_in_days = 10
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.guardduty_kms_policy.json

  tags = merge(local.module_tags, {
    Name = "${local.name_prefix}-guardduty-kms"
  })
}

# Human-readable alias for the KMS key.
resource "aws_kms_alias" "guardduty" {
  name          = "alias/${local.name_prefix}-guardduty"
  target_key_id = aws_kms_key.guardduty.key_id
}

# =============================================================================
# 3. Read-Only IAM Role for Databricks Access
# =============================================================================
# This role trusts the hub role in the security account (not Databricks directly).
# The access chain is: Databricks → hub role → this read-only role → S3 bucket.
# This follows the spoke model where each workload account has a read-only role
# that the centralized hub role can assume.

data "aws_iam_policy_document" "read_only_trust" {
  statement {
    sid    = "TrustHubRole"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = [var.hub_role_arn]
    }
    actions = ["sts:AssumeRole"]
  }
}

resource "aws_iam_role" "read_only" {
  name               = "${local.name_prefix}-read-only-role"
  assume_role_policy = data.aws_iam_policy_document.read_only_trust.json

  tags = merge(local.module_tags, {
    Name = "${local.name_prefix}-read-only-role"
  })
}

# Grant the read-only role access to the security-logs bucket (and optionally
# the host-telemetry bucket). This is the minimum permission set Databricks
# needs to read raw security data and host telemetry via external locations.
data "aws_iam_policy_document" "read_only_policy" {
  statement {
    sid    = "S3ReadAccess"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = concat(
      [
        aws_s3_bucket.security_logs.arn,
        "${aws_s3_bucket.security_logs.arn}/*",
      ],
      # Conditionally include the host telemetry bucket when enabled. The hub
      # role chain-assumes this read-only role, so without these entries the
      # Databricks external location for host telemetry would fail validation.
      var.enable_host_telemetry ? [
        aws_s3_bucket.host_telemetry[0].arn,
        "${aws_s3_bucket.host_telemetry[0].arn}/*",
      ] : [],
    )
  }
}

resource "aws_iam_role_policy" "read_only" {
  name   = "${local.name_prefix}-read-only-s3-policy"
  role   = aws_iam_role.read_only.id
  policy = data.aws_iam_policy_document.read_only_policy.json
}

# =============================================================================
# 4. CloudTrail — Management Events
# =============================================================================
# Single-region trail capturing management events. The trail writes to the
# security-logs bucket under the /cloudtrail/ prefix.
#
# CRITICAL: The bucket policy must exist before the trail. CloudTrail validates
# the bucket policy at creation time and fails with
# InsufficientS3BucketPolicyException if the policy is missing.

resource "aws_cloudtrail" "main" {
  name                          = "${local.name_prefix}-trail"
  s3_bucket_name                = aws_s3_bucket.security_logs.id
  s3_key_prefix                 = "cloudtrail"
  is_multi_region_trail         = false
  enable_log_file_validation    = true
  include_global_service_events = true

  tags = merge(local.module_tags, {
    Name = "${local.name_prefix}-trail"
  })

  # Explicit dependency: bucket policy must be applied before trail creation.
  depends_on = [aws_s3_bucket_policy.security_logs]
}

# =============================================================================
# 5. VPC Flow Logs
# =============================================================================
# Captures ALL traffic (accepted + rejected) from the workload VPC. Logs are
# delivered to S3 under /vpc-flow-logs/ with a custom format that includes
# all useful fields for security analysis.

resource "aws_flow_log" "main" {
  vpc_id               = var.vpc_id
  log_destination_type = "s3"
  log_destination      = "${aws_s3_bucket.security_logs.arn}/vpc-flow-logs"
  traffic_type         = "ALL"

  # 10-minute aggregation window (maximum). Shorter windows increase cost
  # but provide more timely data. 600s is fine for the PoC.
  max_aggregation_interval = 600

  # Custom log format captures all available fields for rich security analysis.
  # The $$ escaping is required by Terraform to produce literal $ in the output.
  log_format = "$${version} $${account-id} $${interface-id} $${srcaddr} $${dstaddr} $${srcport} $${dstport} $${protocol} $${packets} $${bytes} $${start} $${end} $${action} $${log-status} $${vpc-id} $${subnet-id} $${instance-id} $${tcp-flags} $${type} $${pkt-srcaddr} $${pkt-dstaddr} $${region} $${az-id} $${sublocation-type} $${sublocation-id} $${pkt-src-aws-service} $${pkt-dst-aws-service} $${flow-direction}"

  tags = merge(local.module_tags, {
    Name = "${local.name_prefix}-vpc-flow-logs"
  })
}

# =============================================================================
# 6. GuardDuty — Threat Detection
# =============================================================================
# Enables the GuardDuty detector and configures S3 export of findings.
# Findings are exported to /guardduty/ in the security-logs bucket, encrypted
# with the dedicated KMS key.

resource "aws_guardduty_detector" "main" {
  enable                       = true
  finding_publishing_frequency = "FIFTEEN_MINUTES"

  tags = merge(local.module_tags, {
    Name = "${local.name_prefix}-guardduty"
  })
}

resource "aws_guardduty_publishing_destination" "s3" {
  detector_id = aws_guardduty_detector.main.id
  # GuardDuty requires the destination_arn to be the bucket ARN itself — it
  # creates its own prefix structure (e.g., /guardduty/...) automatically.
  destination_arn = aws_s3_bucket.security_logs.arn
  kms_key_arn     = aws_kms_key.guardduty.arn

  # Both the bucket policy and KMS key policy must be in place before creating
  # the publishing destination. GuardDuty validates both at creation time.
  depends_on = [
    aws_s3_bucket_policy.security_logs,
    aws_kms_key.guardduty,
  ]
}

# =============================================================================
# 7. AWS Config — Configuration Recording
# =============================================================================
# AWS Config requires three resources in a strict dependency order:
#   1. Configuration recorder (defines WHAT to record)
#   2. Delivery channel (defines WHERE to send data)
#   3. Recorder status (STARTS the recorder)
#
# The recorder needs an IAM role with the managed AWSConfigServiceRolePolicy.
# AWS only allows ONE Config recorder per region per account.

# --- Config IAM Role ---
# Dedicated role for the Config service with a source account condition
# to prevent confused deputy attacks.
resource "aws_iam_role" "config" {
  name = "${local.name_prefix}-config-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "config.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = var.account_id
        }
      }
    }]
  })

  tags = merge(local.module_tags, {
    Name = "${local.name_prefix}-config-role"
  })
}

# Attach the AWS-managed policy that grants Config the permissions it needs
# to describe resources and deliver configuration items.
resource "aws_iam_role_policy_attachment" "config" {
  role       = aws_iam_role.config.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWS_ConfigRole"
}

# --- Configuration Recorder ---
# Records configuration changes for a targeted set of resource types that
# are relevant to the security lakehouse demo. Using all_supported = false
# keeps costs down and focuses on the resources we actually deploy.
resource "aws_config_configuration_recorder" "main" {
  name     = "default"
  role_arn = aws_iam_role.config.arn

  recording_group {
    all_supported = false
    resource_types = [
      "AWS::EC2::Instance",
      "AWS::EC2::SecurityGroup",
      "AWS::EC2::VPC",
      "AWS::EC2::Subnet",
      "AWS::S3::Bucket",
      "AWS::IAM::Role",
      "AWS::IAM::Policy",
      "AWS::CloudTrail::Trail",
    ]
  }

  # The IAM role policy must be attached before the recorder is created,
  # otherwise Config cannot validate the role has sufficient permissions.
  depends_on = [aws_iam_role_policy_attachment.config]
}

# --- Delivery Channel ---
# Sends configuration snapshots and change notifications to S3.
# Daily snapshots are sufficient for the PoC.
resource "aws_config_delivery_channel" "main" {
  name           = "default"
  s3_bucket_name = aws_s3_bucket.security_logs.id
  s3_key_prefix  = "config"

  snapshot_delivery_properties {
    delivery_frequency = "TwentyFour_Hours"
  }

  # The recorder must exist before the delivery channel (AWS API requirement),
  # and the bucket policy must be in place for Config to validate write access.
  depends_on = [
    aws_config_configuration_recorder.main,
    aws_s3_bucket_policy.security_logs,
  ]
}

# --- Recorder Status ---
# STARTS the recorder. This must be the very last Config resource created.
# CRITICAL: The delivery channel MUST exist before starting the recorder,
# otherwise AWS returns an error that no delivery channel is configured.
resource "aws_config_configuration_recorder_status" "main" {
  name       = aws_config_configuration_recorder.main.name
  is_enabled = true

  depends_on = [aws_config_delivery_channel.main]
}
