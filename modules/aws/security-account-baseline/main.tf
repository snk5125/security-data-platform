# -----------------------------------------------------------------------------
# Security Account Baseline Module
# -----------------------------------------------------------------------------
# Creates the central infrastructure in the management/security account that
# Databricks Unity Catalog requires:
#
#   1. Managed storage S3 bucket  — where Databricks writes Delta tables
#   2. Managed storage IAM role   — assumed by Databricks UC master role
#   3. Hub IAM role               — chains into workload account read-only roles
#
# Both IAM roles follow the Databricks "self-assume" pattern (required since
# Jan 2025): the role trusts itself in addition to the UC master role. The
# trust policies use an external ID that starts as "0000" and is updated
# after Phase 5 creates the Databricks storage credentials.
#
# Prerequisites:
#   - Phase 1 complete (remote backend operational)
#   - AWS Organizations enabled with member accounts created
#   - Caller has IAM permissions in the security account
#
# Resources created: 9  (1 data source, not counted)
# -----------------------------------------------------------------------------

locals {
  managed_storage_role_name = "lakehouse-managed-storage-role"
  hub_role_name             = "lakehouse-hub-role"

  module_tags = merge(var.tags, {
    Component = "security-account-baseline"
  })
}

# ── Data Sources ──────────────────────────────────────────────────────────────

# Reference the existing AWS Organization for org-scoped IAM conditions.
data "aws_organizations_organization" "current" {}

# ═════════════════════════════════════════════════════════════════════════════
# 1. MANAGED STORAGE S3 BUCKET
# ═════════════════════════════════════════════════════════════════════════════
# This bucket is where Databricks writes managed Delta tables. It is separate
# from the Terraform state bucket (created in bootstrap). Databricks requires
# full read/write access to this bucket via the managed storage IAM role.

resource "aws_s3_bucket" "managed_storage" {
  bucket = var.managed_storage_bucket_name

  tags = merge(local.module_tags, {
    Name = var.managed_storage_bucket_name
  })
}

# Enable versioning for Delta table recovery and audit trail.
resource "aws_s3_bucket_versioning" "managed_storage" {
  bucket = aws_s3_bucket.managed_storage.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Encrypt all objects at rest with AES-256 (SSE-S3). Production deployments
# should consider SSE-KMS with a customer-managed key for audit and rotation.
resource "aws_s3_bucket_server_side_encryption_configuration" "managed_storage" {
  bucket = aws_s3_bucket.managed_storage.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Block all public access. Databricks managed storage contains customer data
# and must never be publicly accessible.
resource "aws_s3_bucket_public_access_block" "managed_storage" {
  bucket = aws_s3_bucket.managed_storage.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ── Managed storage bucket policy ────────────────────────────────────────────
# Grants the Databricks UC master role and the managed storage IAM role
# read/write access to the bucket. Both principals need access because:
#   - The UC master role performs initial catalog operations
#   - The managed storage role is what Databricks actually assumes day-to-day

data "aws_iam_policy_document" "managed_storage_bucket_policy" {
  statement {
    sid    = "DatabricksReadWrite"
    effect = "Allow"

    principals {
      type = "AWS"
      identifiers = [
        var.databricks_uc_master_role_arn,
        aws_iam_role.managed_storage.arn,
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

  # The bucket policy references the managed storage role ARN, so the role
  # must exist before the policy is applied.
  depends_on = [aws_iam_role.managed_storage]
}

# ═════════════════════════════════════════════════════════════════════════════
# 2. MANAGED STORAGE IAM ROLE
# ═════════════════════════════════════════════════════════════════════════════
# This role is assumed by the Databricks UC master role to read/write managed
# Delta tables. The trust policy follows the Databricks self-assume pattern:
#   - Trusts the UC master role (Databricks-owned, in account <DATABRICKS_AWS_ACCOUNT_ID>)
#   - Trusts itself (self-assume, required since Jan 2025)
#   - Requires an external ID for security (starts as "0000", updated Phase 5)

data "aws_iam_policy_document" "managed_storage_trust" {
  statement {
    effect = "Allow"

    principals {
      type = "AWS"
      # On first apply (enable_self_assume = false), only the UC master role
      # is trusted. The self-assume principal is added on second apply after
      # the role exists. AWS rejects trust policies that reference non-existent
      # role ARNs, so we must create the role first, then add self-trust.
      identifiers = var.enable_self_assume ? [
        var.databricks_uc_master_role_arn,
        "arn:aws:iam::${var.security_account_id}:role/${local.managed_storage_role_name}",
      ] : [var.databricks_uc_master_role_arn]
    }

    actions = ["sts:AssumeRole"]

    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [var.databricks_storage_credential_external_id]
    }
  }
}

resource "aws_iam_role" "managed_storage" {
  name               = local.managed_storage_role_name
  assume_role_policy = data.aws_iam_policy_document.managed_storage_trust.json

  tags = merge(local.module_tags, {
    Name = local.managed_storage_role_name
  })
}

# ── Managed storage role inline policy ───────────────────────────────────────
# Grants S3 read/write to the managed storage bucket. This is the permission
# that Databricks uses day-to-day when writing Delta tables.

data "aws_iam_policy_document" "managed_storage_role_policy" {
  statement {
    sid    = "ManagedStorageS3Access"
    effect = "Allow"

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

resource "aws_iam_role_policy" "managed_storage" {
  name   = "managed-storage-s3-access"
  role   = aws_iam_role.managed_storage.id
  policy = data.aws_iam_policy_document.managed_storage_role_policy.json
}

# ═════════════════════════════════════════════════════════════════════════════
# 3. HUB IAM ROLE
# ═════════════════════════════════════════════════════════════════════════════
# The hub role is the entry point for Databricks to access data in workload
# accounts. Databricks assumes this role, which then chain-assumes into
# read-only roles in each workload account. This pattern allows a single
# Databricks storage credential to cover multiple AWS accounts.
#
# Trust policy: same self-assume pattern as the managed storage role, but
# with its own external ID (separate credential in Databricks).

data "aws_iam_policy_document" "hub_trust" {
  statement {
    effect = "Allow"

    principals {
      type = "AWS"
      # Same conditional self-assume pattern as the managed storage role.
      # First apply: UC master role only. Second apply: adds self-assume.
      identifiers = var.enable_self_assume ? [
        var.databricks_uc_master_role_arn,
        "arn:aws:iam::${var.security_account_id}:role/${local.hub_role_name}",
      ] : [var.databricks_uc_master_role_arn]
    }

    actions = ["sts:AssumeRole"]

    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [var.databricks_hub_credential_external_id]
    }
  }
}

resource "aws_iam_role" "hub" {
  name               = local.hub_role_name
  assume_role_policy = data.aws_iam_policy_document.hub_trust.json

  tags = merge(local.module_tags, {
    Name = local.hub_role_name
  })
}

# ── Hub role inline policy ───────────────────────────────────────────────────
# Two capabilities:
#   1. STS AssumeRole into lakehouse-read-only roles in workload accounts
#      (scoped to the organization to prevent cross-org access)
#   2. S3 read access to workload security log buckets (for Databricks to
#      ingest CloudTrail, VPC Flow Logs, etc. via external locations)

data "aws_iam_policy_document" "hub_role_policy" {
  # Allow chain-assuming into workload account read-only roles
  statement {
    sid       = "AssumeWorkloadReadOnlyRoles"
    effect    = "Allow"
    actions   = ["sts:AssumeRole"]
    resources = ["arn:aws:iam::*:role/lakehouse-read-only"]

    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalOrgID"
      values   = [var.organization_id]
    }
  }

  # Self-assume: Databricks Unity Catalog requires storage credential IAM
  # roles to be able to assume themselves (required since Jan 2025). The
  # trust policy allows self-assume, but the IAM policy must also permit the
  # sts:AssumeRole action on the role's own ARN. Without this, external
  # location creation fails with "non self-assuming" error.
  statement {
    sid       = "SelfAssume"
    effect    = "Allow"
    actions   = ["sts:AssumeRole"]
    resources = [aws_iam_role.hub.arn]
  }

  # Allow S3 read access to workload security log buckets. The bucket naming
  # convention (*-security-logs-*) is established in Phase 4 (data sources).
  statement {
    sid    = "ReadWorkloadSecurityLogs"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]

    resources = [
      "arn:aws:s3:::*-security-logs-*",
      "arn:aws:s3:::*-security-logs-*/*",
    ]

    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalOrgID"
      values   = [var.organization_id]
    }
  }

  # Allow KMS decrypt for GuardDuty findings. GuardDuty S3 exports use a
  # customer-managed KMS key (required by the GuardDuty service). The hub role
  # must be able to decrypt these files when Databricks reads them via the
  # external location. Other data sources (CloudTrail, VPC Flow, Config) use
  # SSE-S3 and do not require KMS permissions.
  statement {
    sid    = "DecryptGuardDutyFindings"
    effect = "Allow"

    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
    ]

    # Scoped to KMS keys in any account within the organization. The KMS key
    # policy in each workload account further restricts which principals can
    # use the key (defense in depth).
    resources = ["arn:aws:kms:*:*:key/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalOrgID"
      values   = [var.organization_id]
    }
  }
}

resource "aws_iam_role_policy" "hub" {
  name   = "hub-role-chain-assume-and-s3"
  role   = aws_iam_role.hub.id
  policy = data.aws_iam_policy_document.hub_role_policy.json
}
