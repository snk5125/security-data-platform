# -----------------------------------------------------------------------------
# Bootstrap State Backend
# -----------------------------------------------------------------------------
# Creates the S3 bucket and DynamoDB table that serve as the remote backend
# for all subsequent Terraform operations. This configuration itself uses a
# LOCAL backend — its state file (bootstrap/terraform.tfstate) must be
# preserved and should never be deleted.
#
# Why a separate root module? The state backend must exist before any other
# Terraform root can run `terraform init` with an S3 backend. Bootstrapping
# it in its own root with local state avoids the chicken-and-egg problem.
# -----------------------------------------------------------------------------

# ── S3 Bucket (state storage) ───────────────────────────────────────────────

# The bucket that stores all Terraform state files for the project.
# Versioning, encryption, and public-access blocking are configured as
# separate resources below (AWS provider >= 4.x pattern).
resource "aws_s3_bucket" "terraform_state" {
  bucket = "security-lakehouse-tfstate-${data.aws_caller_identity.current.account_id}"

  # Prevent accidental deletion of the state bucket. Remove this lifecycle
  # rule only if you are intentionally tearing down the entire project.
  lifecycle {
    prevent_destroy = true
  }
}

# Enable versioning so that every state write creates a recoverable version.
# This allows rollback if a Terraform apply corrupts state.
resource "aws_s3_bucket_versioning" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Encrypt all objects at rest with AES-256 (SSE-S3). This is the simplest
# encryption option and sufficient for a PoC. Production deployments should
# consider SSE-KMS with a customer-managed key for audit trail and key
# rotation control.
resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Block all public access paths. Terraform state contains sensitive data
# (resource IDs, ARNs, sometimes secrets) and must never be public.
resource "aws_s3_bucket_public_access_block" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ── DynamoDB Table (state locking) ──────────────────────────────────────────

# Provides distributed locking for Terraform operations. When one user or CI
# job is running `terraform apply`, the lock prevents a second concurrent
# apply from corrupting state. The partition key MUST be "LockID" (String) —
# this is the key name the S3 backend expects.
resource "aws_dynamodb_table" "terraform_locks" {
  name         = "security-lakehouse-tflock"
  billing_mode = "PAY_PER_REQUEST" # On-demand — no capacity planning needed for a lock table
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }
}

# ── Data Sources ────────────────────────────────────────────────────────────

# Used to derive the account ID for the state bucket name, ensuring
# uniqueness without requiring the user to supply it as a variable.
data "aws_caller_identity" "current" {}
