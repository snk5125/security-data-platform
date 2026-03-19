# =============================================================================
# IAM Roles — Hub and Managed Storage
# =============================================================================
# These roles bridge Databricks Unity Catalog to AWS. They are created in the
# hub root (not foundation) because their trust policies require external IDs
# that are only known after Databricks storage credentials are created.
#
# Terraform resolves the dependency automatically:
#   1. cloud_integration receives local.hub_role_arn (a string literal)
#   2. No Terraform dependency edge from cloud_integration → IAM roles
#   3. IAM role trust policies reference module.cloud_integration outputs
#   4. Result: credentials created first, then roles. No cycle.

locals {
  hub_role_name             = "lakehouse-hub-role"
  managed_storage_role_name = "lakehouse-managed-storage-role"
  hub_role_arn              = "arn:aws:iam::${var.security_account_id}:role/${local.hub_role_name}"
  managed_storage_role_arn  = "arn:aws:iam::${var.security_account_id}:role/${local.managed_storage_role_name}"
}

# ── Managed Storage Role ─────────────────────────────────────────────────────

data "aws_iam_policy_document" "managed_storage_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type = "AWS"
      identifiers = [
        var.databricks_uc_master_role_arn,
        local.managed_storage_role_arn, # self-assume
      ]
    }

    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [module.cloud_integration.managed_credential_external_id]
    }
  }
}

resource "aws_iam_role" "managed_storage" {
  name               = local.managed_storage_role_name
  assume_role_policy = data.aws_iam_policy_document.managed_storage_trust.json
  tags               = { Purpose = "Databricks Unity Catalog managed storage" }
}

# Inline policy: S3 read/write on managed storage bucket.
resource "aws_iam_role_policy" "managed_storage_s3" {
  name = "managed-storage-s3-access"
  role = aws_iam_role.managed_storage.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation",
        ]
        Resource = [
          var.managed_storage_bucket_arn,
          "${var.managed_storage_bucket_arn}/*",
        ]
      }
    ]
  })
}

# ── Hub Role ─────────────────────────────────────────────────────────────────

data "aws_iam_policy_document" "hub_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type = "AWS"
      identifiers = [
        var.databricks_uc_master_role_arn,
        local.hub_role_arn, # self-assume
      ]
    }

    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [module.cloud_integration.hub_credential_external_id]
    }
  }
}

resource "aws_iam_role" "hub" {
  name               = local.hub_role_name
  assume_role_policy = data.aws_iam_policy_document.hub_trust.json
  tags               = { Purpose = "Databricks Unity Catalog hub cross-account access" }
}

# Inline policy: cross-account assume + S3 read + KMS decrypt.
resource "aws_iam_role_policy" "hub_cross_account" {
  name = "hub-role-chain-assume-and-s3"
  role = aws_iam_role.hub.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AssumeWorkloadReadOnlyRoles"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = "arn:aws:iam::*:role/lakehouse-read-only"
        Condition = {
          StringEquals = {
            "aws:PrincipalOrgID" = var.organization_id
          }
        }
      },
      {
        Sid      = "SelfAssume"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = local.hub_role_arn
      },
      {
        Sid    = "ReadSecurityLogsBuckets"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket",
          "s3:GetBucketLocation",
        ]
        Resource = [
          "arn:aws:s3:::*-security-logs-*",
          "arn:aws:s3:::*-security-logs-*/*",
        ]
        Condition = {
          StringEquals = {
            "aws:PrincipalOrgID" = var.organization_id
          }
        }
      },
      {
        Sid      = "DecryptGuardDutyFindings"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:PrincipalOrgID" = var.organization_id
          }
        }
      }
    ]
  })
}
