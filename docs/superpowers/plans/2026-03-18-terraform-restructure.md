# Terraform Multi-Root Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the monolithic 9-phase Terraform deployment into 4 independent roots (bootstrap, foundation, workloads, hub) with a standardized workload output contract for multi-cloud readiness.

**Architecture:** Split `environments/poc/` into `foundations/aws-security/` (S3, SNS), `workloads/aws-workload-*/` (VPC, EC2, data sources), and `hub/` (IAM roles, Databricks integration, Unity Catalog, jobs). Note: KMS keys live in workload accounts (data-sources module), not the foundation. A shell script (`assemble-workloads.sh`) collects standardized JSON outputs from each workload root into `hub/workloads.auto.tfvars.json`. The hub root creates IAM roles with correct trust policies in a single apply (no two-pass external ID pattern).

**Tech Stack:** Terraform >= 1.5 (HCL), AWS provider ~> 5.50, Databricks provider ~> 1.50, Bash (scripts)

**Spec:** `docs/superpowers/specs/2026-03-18-terraform-restructure-design.md`

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `foundations/aws-security/main.tf` | Foundation root — wires security-foundation module |
| `foundations/aws-security/variables.tf` | Foundation inputs (account ID, org ID, bucket name) |
| `foundations/aws-security/outputs.tf` | Foundation outputs (bucket ARN/name, KMS key ARN, SNS ARN/creds) |
| `foundations/aws-security/providers.tf` | Single AWS provider (security account) |
| `foundations/aws-security/versions.tf` | Terraform + provider version constraints |
| `foundations/aws-security/backend.tf.example` | S3 backend config template |
| `foundations/aws-security/terraform.tfvars.example` | Variable values template |
| `modules/aws/security-foundation/main.tf` | S3 managed storage bucket + SNS topic + publisher IAM |
| `modules/aws/security-foundation/variables.tf` | Inputs for foundation resources |
| `modules/aws/security-foundation/outputs.tf` | Bucket, SNS, and credential outputs |
| `hub/iam.tf` | Hub + managed-storage IAM roles (inline, deterministic ARNs break circular dep) |
| `workloads/_template-aws/main.tf` | Template root — wires baseline + data-sources modules |
| `workloads/_template-aws/variables.tf` | Template inputs |
| `workloads/_template-aws/outputs.tf` | Standardized workload output contract |
| `workloads/_template-aws/providers.tf` | Single AWS provider (workload account) |
| `workloads/_template-aws/versions.tf` | Version constraints |
| `workloads/_template-aws/backend.tf.example` | S3 backend template |
| `workloads/_template-aws/terraform.tfvars.example` | Variable values template |
| `workloads/aws-workload-a/` | Copy of template with workload-a values |
| `workloads/aws-workload-b/` | Copy of template with workload-b values |
| `hub/main.tf` | Hub root — IAM roles, cloud integration, Unity Catalog, jobs |
| `hub/variables.tf` | Hub inputs (workloads list, foundation outputs, Databricks config) |
| `hub/outputs.tf` | Hub outputs (credential names, job IDs, external IDs) |
| `hub/providers.tf` | Databricks + AWS (security account) providers |
| `hub/versions.tf` | Version constraints |
| `hub/backend.tf.example` | S3 backend template |
| `notebooks/bronze/aws/` | Moved from `notebooks/bronze/` |
| `notebooks/security/threat_intel/` | Moved from `notebooks/threat_intel/` |
| `scripts/assemble-workloads.sh` | Collects workload outputs → hub tfvars |
| `scripts/apply-all.sh` | Orchestrates full deploy sequence |
| `scripts/migrate-state.sh` | One-time state migration helper |

### Modified Files

| File | Change |
|------|--------|
| `modules/databricks/cloud-integration/main.tf` | Replace hardcoded workload_a/b with `for_each` on workloads |
| `modules/databricks/cloud-integration/variables.tf` | Replace per-workload vars with `workloads` list |
| `modules/databricks/cloud-integration/outputs.tf` | Dynamic workload external location outputs |
| `modules/databricks/unity-catalog/main.tf` | Add `security` schema |
| `modules/databricks/unity-catalog/variables.tf` | Add `extra_schemas` variable |
| `modules/databricks/unity-catalog/outputs.tf` | Add `security_schema_name` output |
| `modules/databricks/jobs/variables.tf` | Update default notebook source paths |
| `modules/databricks/jobs/main.tf` | Update notebook directory structure references |

### Deprecated (retained for reference)

| File | Status |
|------|--------|
| `environments/poc/` | Entire directory deprecated after migration |
| `modules/aws/security-account-baseline/` | Replaced by security-foundation + inline IAM in hub/iam.tf |
| `modules/aws/sns-alerts/` | Absorbed into security-foundation |
| `onboard_workload_account.sh` | Replaced by template-copy workflow |

---

## Task Dependency Graph

```
Task 1 (scaffolding)
  ├── Task 2 (security-foundation module) ──┐
  ├── Task 3 (inline IAM in hub/iam.tf) ────┤
  ├── Task 4 (cloud-integration for_each) ──┤
  ├── Task 5 (unity-catalog security schema)┤
  ├── Task 6 (notebook reorg + table refs) ─┤
  │                                         │
  ├── Task 7 (foundation root) ─────────────┤ (needs Task 2)
  ├── Task 8 (workload template + roots) ───┤
  ├── Task 9 (hub root) ───────────────────┤ (needs Tasks 3,4,5)
  │                                         │
  ├── Task 10 (jobs module path updates) ───┤ (needs Task 6)
  ├── Task 11 (scripts) ───────────────────┤ (needs Tasks 7,8,9)
  ├── Task 12 (validate all roots) ─────────┤ (needs all above)
  └── Task 13 (docs + cleanup) ────────────── (needs Task 12)
```

Tasks 2-6 are independent and can be parallelized. Tasks 7-9 depend on their respective modules. Tasks 10-13 are sequential.

**Note:** After Task 4 (cloud-integration for_each), the `environments/poc/` root will be broken because it still passes the old per-workload variables. This is expected — `environments/poc/` is deprecated after migration. Do not run `terraform validate` on the old root after this point.

---

### Task 1: Create Directory Scaffolding

**Files:**
- Create: `foundations/aws-security/` (empty dir)
- Create: `modules/aws/security-foundation/` (empty dir)
- Create: `workloads/_template-aws/` (empty dir)
- Create: `workloads/aws-workload-a/` (empty dir)
- Create: `workloads/aws-workload-b/` (empty dir)
- Create: `hub/` (empty dir)
- Create: `notebooks/bronze/aws/` (empty dir)
- Create: `notebooks/security/threat_intel/` (empty dir)
- Create: `scripts/` (empty dir)

- [ ] **Step 1: Create all new directories**

```bash
mkdir -p foundations/aws-security
mkdir -p modules/aws/security-foundation
mkdir -p workloads/_template-aws
mkdir -p workloads/aws-workload-a
mkdir -p workloads/aws-workload-b
mkdir -p hub
mkdir -p notebooks/bronze/aws
mkdir -p notebooks/security/threat_intel
mkdir -p scripts
```

- [ ] **Step 2: Add generated files to .gitignore**

```bash
# Add hub/workloads.auto.tfvars.json and state migration backup to .gitignore
cat >> .gitignore << 'EOF'

# =============================================================================
# Multi-root generated files
# =============================================================================

# Generated by assemble-workloads.sh — contains workload outputs
hub/workloads.auto.tfvars.json

# State migration backups
.state-migration-backup/
EOF
```

- [ ] **Step 3: Commit scaffolding**

```bash
# Add .gitkeep files to empty dirs so git tracks them
for d in foundations/aws-security modules/aws/security-foundation \
         workloads/_template-aws workloads/aws-workload-a workloads/aws-workload-b \
         hub notebooks/bronze/aws notebooks/security/threat_intel scripts; do
  touch "$d/.gitkeep"
done
git add -A
git commit -m "chore: scaffold directory structure for multi-root restructure"
```

---

### Task 2: Create `security-foundation` Module

Extract S3 managed storage bucket and SNS resources from `security-account-baseline` and `sns-alerts` into a new combined module.

**Files:**
- Create: `modules/aws/security-foundation/main.tf`
- Create: `modules/aws/security-foundation/variables.tf`
- Create: `modules/aws/security-foundation/outputs.tf`
- Create: `modules/aws/security-foundation/versions.tf`
- Reference: `modules/aws/security-account-baseline/main.tf` (lines 1-135 — S3 bucket section)
- Reference: `modules/aws/sns-alerts/main.tf` (entire file)

- [ ] **Step 1: Write `modules/aws/security-foundation/variables.tf`**

```hcl
# Variables — Security Foundation Module
# Inputs for the managed storage S3 bucket and SNS alert infrastructure.

variable "security_account_id" {
  description = "AWS account ID of the security/management account."
  type        = string
}

variable "organization_id" {
  description = "AWS Organizations ID — scopes IAM conditions to the organization."
  type        = string
}

variable "managed_storage_bucket_name" {
  description = "Name for the managed storage S3 bucket (Delta tables)."
  type        = string
}

variable "databricks_uc_master_role_arn" {
  description = "Databricks Unity Catalog master role ARN (Databricks-owned account)."
  type        = string
  default     = "arn:aws:iam::<DATABRICKS_AWS_ACCOUNT_ID>:role/unity-catalog-prod-UCMasterRole-<SUFFIX>"
}

# Hub and managed-storage role names must match what hub-iam module creates.
# These are used in the S3 bucket policy to pre-authorize the roles before
# they exist (ARN-based policies don't require principals to exist).
variable "hub_role_name" {
  description = "Name of the hub IAM role (created by the hub root). Used in bucket policy."
  type        = string
  default     = "security-lakehouse-hub"
}

variable "managed_storage_role_name" {
  description = "Name of the managed storage IAM role (created by the hub root). Used in bucket policy."
  type        = string
  default     = "security-lakehouse-managed-storage"
}

variable "sns_topic_name" {
  description = "Name for the SNS alert topic."
  type        = string
  default     = "security-lakehouse-alerts"
}

variable "sns_publisher_iam_user_name" {
  description = "Name for the IAM user that publishes to SNS."
  type        = string
  default     = "lakehouse-sns-publisher"
}

variable "tags" {
  description = "Additional tags to apply to all resources."
  type        = map(string)
  default     = {}
}
```

- [ ] **Step 2: Write `modules/aws/security-foundation/main.tf`**

Copy the S3 bucket section (lines 1-135) from `modules/aws/security-account-baseline/main.tf` and the entire `modules/aws/sns-alerts/main.tf`. Adapt:

- Replace `local.managed_storage_role_name` references with `var.managed_storage_role_name`
- Replace `local.hub_role_name` references with `var.hub_role_name`
- Construct deterministic role ARNs using `arn:aws:iam::${var.security_account_id}:role/${var.hub_role_name}` (roles don't need to exist for bucket policy)
- Include the SNS topic, topic policy, IAM user, user policy, and access key from sns-alerts

Key sections to include:
1. S3 bucket with versioning, SSE-S3, public access block
2. S3 bucket policy granting read/write to managed-storage role and UC master role
3. SNS topic (standard, not FIFO)
4. SNS topic policy (restricts publisher)
5. IAM user for SNS publishing (path: `/databricks/`)
6. IAM user inline policy (sns:Publish on topic ARN only)
7. IAM access key for the publisher user

- [ ] **Step 3: Write `modules/aws/security-foundation/outputs.tf`**

```hcl
# Outputs — Security Foundation Module

output "managed_storage_bucket_arn" {
  description = "Managed storage S3 bucket ARN."
  value       = aws_s3_bucket.managed_storage.arn
}

output "managed_storage_bucket_name" {
  description = "Managed storage S3 bucket name."
  value       = aws_s3_bucket.managed_storage.id
}

output "sns_topic_arn" {
  description = "SNS topic ARN for alert forwarding."
  value       = aws_sns_topic.alerts.arn
}

output "sns_topic_name" {
  description = "SNS topic name."
  value       = aws_sns_topic.alerts.name
}

output "sns_publisher_access_key_id" {
  description = "Access key ID for the SNS publisher IAM user."
  value       = aws_iam_access_key.sns_publisher.id
}

output "sns_publisher_secret_access_key" {
  description = "Secret access key for the SNS publisher IAM user."
  value       = aws_iam_access_key.sns_publisher.secret
  sensitive   = true
}

output "sns_publisher_iam_user_arn" {
  description = "IAM user ARN for the SNS publisher."
  value       = aws_iam_user.sns_publisher.arn
}
```

- [ ] **Step 4: Write `modules/aws/security-foundation/versions.tf`**

```hcl
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
  }
}
```

- [ ] **Step 5: Validate module**

```bash
cd modules/aws/security-foundation
terraform fmt -check -recursive .
terraform init -backend=false
terraform validate
```

Expected: All pass with no errors.

- [ ] **Step 6: Commit**

```bash
git add modules/aws/security-foundation/
git commit -m "feat: create security-foundation module (S3 + SNS extracted)"
```

---

### Task 3: Create Inline IAM Resources for Hub Root

The IAM roles cannot live in a separate module from the Databricks storage credentials because of a circular dependency: storage credentials need role ARNs, and role trust policies need Databricks-assigned external IDs. The solution is a **two-phase inline pattern** in `hub/main.tf`:

1. **Phase A:** Create IAM roles with a baseline trust policy (trusts only the UC master role, no external ID condition)
2. **Phase B:** Create Databricks storage credentials (which output external IDs) and update the IAM trust policies via `aws_iam_role_policy` to add external ID conditions and self-assume

This avoids the circular dependency because Phase A roles don't reference Databricks resources, and Phase B trust policy updates reference the storage credential outputs.

**Files:**
- Create: `hub/iam.tf` — IAM roles and trust policy updates (inline in hub root, not a module)
- Reference: `modules/aws/security-account-baseline/main.tf` (lines 137-336)

**Note:** The `modules/aws/hub-iam/` directory from Task 1 scaffolding is no longer needed. The IAM resources are inlined directly in the hub root to avoid the circular module dependency.

- [ ] **Step 1: Design the two-phase IAM pattern**

Read `modules/aws/security-account-baseline/main.tf` lines 137-336 to understand the current IAM role structure. Note the trust policy documents, inline policies, and the self-assume pattern.

The key insight: `aws_iam_role.assume_role_policy` is the trust policy set at role creation. We can create the roles with a minimal trust policy (UC master role only), then use the storage credential external IDs to create an updated trust policy. Terraform's `aws_iam_role` resource will update the `assume_role_policy` in-place when it changes.

The actual implementation: use `locals` to compute the trust policy dynamically. The storage credentials are created in the same root — Terraform will sequence them correctly because `hub_credential_external_id` is an attribute of the storage credential resource.

- [ ] **Step 2: Write `hub/iam.tf`**

```hcl
# =============================================================================
# IAM Roles — Hub and Managed Storage
# =============================================================================
# These roles bridge Databricks Unity Catalog to AWS. They are created in the
# hub root (not foundation) because their trust policies require external IDs
# that are only known after Databricks storage credentials are created.
#
# Terraform resolves the dependency automatically:
#   1. Create roles with trust policy referencing storage credential external IDs
#   2. Terraform sees the dependency and creates storage credentials first
#      (via cloud_integration module), then creates/updates the roles
#
# Wait — that's still circular. The storage credentials need the role ARNs.
#
# ACTUAL resolution: Create the roles with a COMPUTED trust policy that uses
# the external IDs. Terraform will:
#   1. See that aws_iam_role.hub depends on module.cloud_integration (for external ID)
#   2. See that module.cloud_integration depends on aws_iam_role.hub (for role ARN)
#   3. Detect the cycle and fail.
#
# REAL resolution (no cycle): Use well-known deterministic role ARNs.
# The role names are fixed strings. We pass the DETERMINISTIC ARN to the
# cloud_integration module (not the aws_iam_role output), breaking the cycle.
# The storage credentials don't actually need the role to exist yet — they
# just need the ARN string to register. The roles are created in the same
# apply, so they exist by the time Databricks validates them.

locals {
  hub_role_name             = "security-lakehouse-hub"
  managed_storage_role_name = "security-lakehouse-managed-storage"
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
  name   = "managed-storage-s3-access"
  role   = aws_iam_role.managed_storage.id
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
  name   = "hub-cross-account-access"
  role   = aws_iam_role.hub.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AssumeWorkloadReadOnlyRoles"
        Effect = "Allow"
        Action = "sts:AssumeRole"
        Resource = "arn:aws:iam::*:role/lakehouse-read-only"
        Condition = {
          StringEquals = {
            "aws:PrincipalOrgID" = var.organization_id
          }
        }
      },
      {
        Sid    = "SelfAssume"
        Effect = "Allow"
        Action = "sts:AssumeRole"
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
        Sid    = "DecryptGuardDutyFindings"
        Effect = "Allow"
        Action = "kms:Decrypt"
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
```

**KEY INSIGHT:** The `data.aws_iam_policy_document` resources reference `module.cloud_integration.hub_credential_external_id`, which creates a dependency on the cloud_integration module. But the `module.cloud_integration` receives `local.hub_role_arn` (a computed string, NOT `aws_iam_role.hub.arn`), so there is NO dependency from cloud_integration back to the IAM role. This breaks the cycle:

```
cloud_integration → (no dependency) → created first (outputs external IDs)
aws_iam_role.hub → depends on cloud_integration (for external ID) → created second
```

Terraform will: create storage credentials → get external IDs → create IAM roles with correct trust policies. All in one apply.

- [ ] **Step 3: Validate the dependency graph mentally**

Verify no cycle:
- `module.cloud_integration` inputs: `hub_role_arn = local.hub_role_arn` (string literal, no resource dependency)
- `aws_iam_role.hub` inputs: `assume_role_policy` references `module.cloud_integration.hub_credential_external_id` (resource dependency)
- Result: cloud_integration created first, IAM roles created second. No cycle.

- [ ] **Step 4: Remove `modules/aws/hub-iam/` from scaffolding**

The hub-iam module is no longer needed since IAM resources are inlined in `hub/iam.tf`.

```bash
rm -rf modules/aws/hub-iam
```

- [ ] **Step 5: Commit**

```bash
git add hub/iam.tf
git rm -rf modules/aws/hub-iam/ 2>/dev/null || true
git commit -m "feat: inline IAM roles in hub root with deterministic ARNs (no circular dep)"
```

---

### Task 4: Refactor `cloud-integration` Module for `for_each`

Replace hardcoded `workload_a` / `workload_b` external locations with a dynamic `for_each` on a workloads list.

**Files:**
- Modify: `modules/databricks/cloud-integration/main.tf`
- Modify: `modules/databricks/cloud-integration/variables.tf`
- Modify: `modules/databricks/cloud-integration/outputs.tf`

- [ ] **Step 1: Read current module files**

Read all three files to understand current resource names and structure:
- `modules/databricks/cloud-integration/main.tf`
- `modules/databricks/cloud-integration/variables.tf`
- `modules/databricks/cloud-integration/outputs.tf`

- [ ] **Step 2: Update `variables.tf`**

Replace the per-workload bucket name variables with a single `workloads` list:

```hcl
# Replace these variables:
#   workload_a_security_logs_bucket_name
#   workload_b_security_logs_bucket_name
# With:

variable "workloads" {
  description = "List of workload manifests from assemble-workloads.sh. Each entry describes one workload's storage and data products."
  type = list(object({
    alias   = string
    cloud   = string
    storage = object({
      type        = string
      bucket_name = string
      bucket_arn  = string
    })
    read_only_role_arn = string
  }))
}
```

Keep existing variables: `hub_role_arn`, `managed_storage_role_arn`, `managed_storage_bucket_name`.

- [ ] **Step 3: Update `main.tf` — external locations**

Replace the two hardcoded `databricks_external_location` resources with a single `for_each`:

```hcl
# Replace:
#   resource "databricks_external_location" "workload_a_security_logs" { ... }
#   resource "databricks_external_location" "workload_b_security_logs" { ... }
# With:

resource "databricks_external_location" "workload" {
  for_each = { for w in var.workloads : w.alias => w }

  name            = "security-logs-${each.key}"
  url             = "s3://${each.value.storage.bucket_name}"
  credential_name = databricks_storage_credential.hub.name
  read_only       = true
  comment         = "Security logs for ${each.key} (${each.value.cloud})"

  depends_on = [databricks_storage_credential.hub]
}
```

Keep the managed storage external location as a singleton (it's not per-workload).
Keep both storage credentials as singletons (hub + managed).

- [ ] **Step 4: Update `outputs.tf`**

Replace per-workload outputs with a dynamic map:

```hcl
# Replace:
#   output "workload_a_external_location_url" { ... }
#   output "workload_b_external_location_url" { ... }
# With:

output "workload_external_location_urls" {
  description = "Map of workload alias → external location URL."
  value       = { for k, v in databricks_external_location.workload : k => v.url }
}
```

Keep existing outputs: `hub_credential_name`, `managed_credential_name`, `hub_credential_external_id`, `managed_credential_external_id`, `managed_external_location_url`.

- [ ] **Step 5: Format and validate**

```bash
cd modules/databricks/cloud-integration
terraform fmt .
terraform init -backend=false
terraform validate
```

Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add modules/databricks/cloud-integration/
git commit -m "refactor: cloud-integration uses for_each on workloads list"
```

---

### Task 5: Add `security` Schema to Unity Catalog Module

**Files:**
- Modify: `modules/databricks/unity-catalog/main.tf`
- Modify: `modules/databricks/unity-catalog/variables.tf`
- Modify: `modules/databricks/unity-catalog/outputs.tf`

- [ ] **Step 1: Read current module files**

Read `modules/databricks/unity-catalog/main.tf`, `variables.tf`, `outputs.tf`.

- [ ] **Step 2: Add `extra_schemas` variable to `variables.tf`**

```hcl
variable "extra_schemas" {
  description = "Additional schemas to create beyond the medallion layers (bronze/silver/gold)."
  type        = list(string)
  default     = []
}
```

- [ ] **Step 3: Add dynamic schema resource to `main.tf`**

After the existing bronze/silver/gold schema resources, add:

```hcl
# ── Additional Schemas ───────────────────────────────────────────────────────
# Domain-specific schemas beyond the medallion layers (e.g., "security" for
# threat intel, "operations" for cost/performance). Created dynamically from
# the extra_schemas variable.

resource "databricks_schema" "extra" {
  for_each = toset(var.extra_schemas)

  catalog_name = databricks_catalog.this.name
  name         = each.value
  comment      = "${each.value} schema"

  depends_on = [databricks_catalog.this]
}

resource "databricks_grants" "extra_schema" {
  for_each = toset(var.extra_schemas)

  schema = "${databricks_catalog.this.name}.${each.value}"

  grant {
    principal  = "account users"
    privileges = ["USE_SCHEMA", "CREATE_TABLE", "CREATE_FUNCTION"]
  }

  depends_on = [databricks_schema.extra]
}
```

- [ ] **Step 4: Add output for extra schemas**

```hcl
output "extra_schema_names" {
  description = "Map of extra schema names."
  value       = { for k, v in databricks_schema.extra : k => v.name }
}
```

- [ ] **Step 5: Format and validate**

```bash
cd modules/databricks/unity-catalog
terraform fmt .
terraform init -backend=false
terraform validate
```

- [ ] **Step 6: Commit**

```bash
git add modules/databricks/unity-catalog/
git commit -m "feat: add extra_schemas variable to unity-catalog module"
```

---

### Task 6: Reorganize Notebooks

Move bronze notebooks under `bronze/aws/` and threat intel notebooks under `security/threat_intel/`.

**Files:**
- Move: `notebooks/bronze/*.py` → `notebooks/bronze/aws/`
- Move: `notebooks/threat_intel/*.py` → `notebooks/security/threat_intel/`

- [ ] **Step 1: Move bronze notebooks**

```bash
# Move all bronze notebooks into the aws/ subdirectory
mv notebooks/bronze/00_ocsf_common.py notebooks/bronze/aws/
mv notebooks/bronze/01_bronze_cloudtrail.py notebooks/bronze/aws/01_cloudtrail.py
mv notebooks/bronze/02_bronze_vpc_flow.py notebooks/bronze/aws/02_vpc_flow.py
mv notebooks/bronze/03_bronze_guardduty.py notebooks/bronze/aws/03_guardduty.py
mv notebooks/bronze/04_bronze_config.py notebooks/bronze/aws/04_config.py
```

- [ ] **Step 2: Move threat intel notebooks**

```bash
mv notebooks/threat_intel/00_threat_intel_common.py notebooks/security/threat_intel/
mv notebooks/threat_intel/01_bronze_threat_intel_ingest.py notebooks/security/threat_intel/01_bronze_ingest.py
mv notebooks/threat_intel/02_silver_threat_intel_network.py notebooks/security/threat_intel/02_silver_network.py
```

- [ ] **Step 3: Remove empty directories and .gitkeep files**

```bash
rmdir notebooks/threat_intel 2>/dev/null || true
rm -f notebooks/bronze/aws/.gitkeep notebooks/security/threat_intel/.gitkeep
```

- [ ] **Step 4: Update threat intel table references in notebooks**

The threat intel tables are moving from `bronze.threat_intel_raw` / `silver.threat_intel_network` to `security.threat_intel_raw` / `security.threat_intel_network`. Update all references:

```bash
# Find and update all threat_intel table references
grep -rl "bronze\.threat_intel_raw\|bronze\.threat_intel" notebooks/ --include="*.py"
grep -rl "silver\.threat_intel_network\|silver\.threat_intel" notebooks/ --include="*.py"

# Apply replacements (verify the grep output first):
#   bronze.threat_intel_raw → security.threat_intel_raw
#   silver.threat_intel_network → security.threat_intel_network
# Also check notebooks/gold/02_gold_alerts.py which reads silver.threat_intel_network for IOC correlation
```

Manually verify each replacement is correct — some references may be in comments or strings that should also be updated.

- [ ] **Step 5: Verify `%run` import paths in notebooks**

Read the first few lines of each notebook to check for `%run` imports:
- `notebooks/bronze/aws/01_cloudtrail.py` should have `%run ./00_ocsf_common` — still correct (same directory)
- `notebooks/security/threat_intel/01_bronze_ingest.py` should have `%run ./00_threat_intel_common` — still correct (same directory)

If any `%run` paths reference parent directories or absolute workspace paths, update them.

- [ ] **Step 6: Verify no other files reference old notebook paths**

```bash
grep -r "notebooks/bronze/01_bronze" . --include="*.tf" --include="*.py" --include="*.sh" --include="*.md"
grep -r "notebooks/threat_intel/" . --include="*.tf" --include="*.py" --include="*.sh" --include="*.md"
```

Expected: Hits in `modules/databricks/jobs/main.tf` (updated in Task 10), `environments/poc/` (deprecated), docs, and CLAUDE.md. Note which files need updating.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: reorganize notebooks — bronze/aws/ and security/threat_intel/"
```

---

### Task 7: Create Foundation Root (`foundations/aws-security/`)

**Files:**
- Create: `foundations/aws-security/main.tf`
- Create: `foundations/aws-security/variables.tf`
- Create: `foundations/aws-security/outputs.tf`
- Create: `foundations/aws-security/providers.tf`
- Create: `foundations/aws-security/versions.tf`
- Create: `foundations/aws-security/backend.tf.example`
- Create: `foundations/aws-security/terraform.tfvars.example`

- [ ] **Step 1: Write `versions.tf`**

```hcl
terraform {
  required_version = ">= 1.5, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
  }
}
```

- [ ] **Step 2: Write `providers.tf`**

```hcl
# Provider Configuration — Foundation Root
# Single AWS provider targeting the security/management account.
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "security-lakehouse"
      Environment = "poc"
      ManagedBy   = "terraform"
    }
  }
}
```

- [ ] **Step 3: Write `variables.tf`**

```hcl
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
```

- [ ] **Step 4: Write `main.tf`**

```hcl
# Foundation Root — Security Account
# Creates the managed storage bucket and SNS alert infrastructure in the
# security account. IAM roles are created by the hub root (which has access
# to Databricks-assigned external IDs).

module "security_foundation" {
  source = "../../modules/aws/security-foundation"

  security_account_id         = var.security_account_id
  organization_id             = var.organization_id
  managed_storage_bucket_name = var.managed_storage_bucket_name
}
```

- [ ] **Step 5: Write `outputs.tf`**

```hcl
# Outputs — Foundation Root
# Consumed by workload roots (via remote state) and hub root (via assemble script).

output "managed_storage_bucket_arn" {
  description = "Managed storage S3 bucket ARN."
  value       = module.security_foundation.managed_storage_bucket_arn
}

output "managed_storage_bucket_name" {
  description = "Managed storage S3 bucket name."
  value       = module.security_foundation.managed_storage_bucket_name
}

output "sns_topic_arn" {
  description = "SNS topic ARN for alert forwarding."
  value       = module.security_foundation.sns_topic_arn
}

output "sns_publisher_access_key_id" {
  description = "Access key ID for the SNS publisher."
  value       = module.security_foundation.sns_publisher_access_key_id
}

output "sns_publisher_secret_access_key" {
  description = "Secret access key for the SNS publisher."
  value       = module.security_foundation.sns_publisher_secret_access_key
  sensitive   = true
}

output "aws_region" {
  description = "AWS region (passed through for hub consumption)."
  value       = var.aws_region
}
```

- [ ] **Step 6: Write `backend.tf.example` and `terraform.tfvars.example`**

```hcl
# backend.tf.example
terraform {
  backend "s3" {
    bucket         = "security-lakehouse-tfstate-<SECURITY_ACCOUNT_ID>"
    key            = "foundations/aws-security/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "security-lakehouse-tflock"
    encrypt        = true
  }
}
```

```hcl
# terraform.tfvars.example
aws_region                  = "us-east-1"
security_account_id         = "<SECURITY_ACCOUNT_ID>"
organization_id             = "<ORGANIZATION_ID>"
managed_storage_bucket_name = "security-lakehouse-managed-<SECURITY_ACCOUNT_ID>"
```

- [ ] **Step 7: Format and validate**

```bash
cd foundations/aws-security
terraform fmt -check -recursive .
terraform init -backend=false
terraform validate
```

- [ ] **Step 8: Commit**

```bash
git add foundations/aws-security/
git commit -m "feat: create foundation root (aws-security)"
```

---

### Task 8: Create Workload Template and Roots

**Files:**
- Create: `workloads/_template-aws/main.tf`
- Create: `workloads/_template-aws/variables.tf`
- Create: `workloads/_template-aws/outputs.tf`
- Create: `workloads/_template-aws/providers.tf`
- Create: `workloads/_template-aws/versions.tf`
- Create: `workloads/_template-aws/backend.tf.example`
- Create: `workloads/_template-aws/terraform.tfvars.example`
- Create: `workloads/aws-workload-a/` (copy of template with values)
- Create: `workloads/aws-workload-b/` (copy of template with values)

- [ ] **Step 1: Write `_template-aws/versions.tf`**

```hcl
terraform {
  required_version = ">= 1.5, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}
```

- [ ] **Step 2: Write `_template-aws/providers.tf`**

```hcl
# Provider Configuration — AWS Workload Root
# Single AWS provider targeting this workload account via
# OrganizationAccountAccessRole. Credentials from the security account
# session chain-assume into the workload account.

provider "aws" {
  region = var.aws_region

  assume_role {
    role_arn = "arn:aws:iam::${var.account_id}:role/OrganizationAccountAccessRole"
  }

  default_tags {
    tags = {
      Project     = "security-lakehouse"
      Environment = "poc"
      ManagedBy   = "terraform"
    }
  }
}
```

- [ ] **Step 3: Write `_template-aws/variables.tf`**

```hcl
variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "account_alias" {
  description = "Short alias for this workload account (e.g., 'workload-a')."
  type        = string
}

variable "account_id" {
  description = "AWS account ID for this workload account."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the workload VPC."
  type        = string
}

variable "public_subnet_cidr" {
  description = "CIDR block for the public subnet (must be within vpc_cidr)."
  type        = string
}

variable "security_account_id" {
  description = "Security account ID — used to construct the deterministic hub role ARN."
  type        = string
}

variable "hub_role_name" {
  description = "Hub IAM role name. Must match the role created by the hub root."
  type        = string
  default     = "security-lakehouse-hub"
}
```

- [ ] **Step 4: Write `_template-aws/main.tf`**

```hcl
# Workload Root — AWS Account
# Deploys VPC, EC2, and security data sources in a single workload account.
# Combines workload-account-baseline and data-sources modules.

module "baseline" {
  source = "../../modules/aws/workload-account-baseline"

  account_alias      = var.account_alias
  account_id         = var.account_id
  vpc_cidr           = var.vpc_cidr
  public_subnet_cidr = var.public_subnet_cidr
}

module "data_sources" {
  source = "../../modules/aws/data-sources"

  account_alias = var.account_alias
  account_id    = var.account_id
  region        = var.aws_region
  vpc_id        = module.baseline.vpc_id
  # Deterministic hub role ARN — the role may not exist yet (created by hub root
  # in Step 4), but ARN-based trust policies don't require the principal to exist.
  # This is the same pattern used by the foundation S3 bucket policy.
  hub_role_arn  = "arn:aws:iam::${var.security_account_id}:role/${var.hub_role_name}"
}
```

- [ ] **Step 5: Write `_template-aws/outputs.tf` — the workload contract**

```hcl
# Outputs — Workload Contract
# Every workload root exports a standardized JSON manifest consumed by
# assemble-workloads.sh → hub/workloads.auto.tfvars.json.

output "workload_manifest" {
  description = "Standardized workload output contract for hub consumption."
  value = {
    cloud      = "aws"
    account_id = var.account_id
    alias      = var.account_alias
    region     = var.aws_region
    storage = {
      type        = "s3"
      bucket_name = module.data_sources.security_logs_bucket_name
      bucket_arn  = module.data_sources.security_logs_bucket_arn
    }
    read_only_role_arn = module.data_sources.read_only_role_arn
    encryption = {
      type    = "kms"
      key_arn = module.data_sources.kms_key_arn
    }
    data_products = {
      network_traffic = {
        format      = "json"
        path_prefix = "vpc-flow-logs/"
      }
      management_plane = {
        format      = "json"
        path_prefix = "cloudtrail/"
      }
      threat_detection = {
        format      = "json"
        path_prefix = "guardduty/"
      }
      resource_inventory = {
        format      = "json"
        path_prefix = "config/"
      }
    }
  }
}

# Pass-through outputs for convenience / debugging.
output "vpc_id" {
  description = "Workload VPC ID."
  value       = module.baseline.vpc_id
}

output "security_logs_bucket_name" {
  description = "Security logs S3 bucket name."
  value       = module.data_sources.security_logs_bucket_name
}
```

- [ ] **Step 6: Write `backend.tf.example` and `terraform.tfvars.example`**

```hcl
# backend.tf.example
terraform {
  backend "s3" {
    bucket         = "security-lakehouse-tfstate-<SECURITY_ACCOUNT_ID>"
    key            = "workloads/<ACCOUNT_ALIAS>/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "security-lakehouse-tflock"
    encrypt        = true
  }
}
```

```hcl
# terraform.tfvars.example
aws_region         = "us-east-1"
account_alias      = "<ACCOUNT_ALIAS>"          # e.g., "workload-a"
account_id         = "<WORKLOAD_ACCOUNT_ID>"    # 12-digit AWS account ID
vpc_cidr           = "10.0.0.0/16"
public_subnet_cidr = "10.0.1.0/24"
security_account_id = "<SECURITY_ACCOUNT_ID>"    # For deterministic hub role ARN
```

- [ ] **Step 7: Format and validate the template**

```bash
cd workloads/_template-aws
terraform fmt -check -recursive .
terraform init -backend=false
terraform validate
```

- [ ] **Step 8: Create `aws-workload-a` from template**

```bash
cp -r workloads/_template-aws/* workloads/aws-workload-a/
# The terraform.tfvars for workload-a will use:
#   account_alias = "workload-a"
#   vpc_cidr = "10.0.0.0/16"
#   public_subnet_cidr = "10.0.1.0/24"
```

- [ ] **Step 9: Create `aws-workload-b` from template**

```bash
cp -r workloads/_template-aws/* workloads/aws-workload-b/
# The terraform.tfvars for workload-b will use:
#   account_alias = "workload-b"
#   vpc_cidr = "10.1.0.0/16"
#   public_subnet_cidr = "10.1.1.0/24"
```

- [ ] **Step 10: Commit**

```bash
git add workloads/
git commit -m "feat: create AWS workload template and instantiate workload-a/b roots"
```

---

### Task 9: Create Hub Root

The hub root is the most complex — it wires IAM roles, Databricks integration, Unity Catalog, workspace config, and jobs.

**Files:**
- Create: `hub/main.tf`
- Create: `hub/variables.tf`
- Create: `hub/outputs.tf`
- Create: `hub/providers.tf`
- Create: `hub/versions.tf`
- Create: `hub/backend.tf.example`

Note: `hub/data.tf` (remote state) is not needed — foundation outputs are passed via `terraform.tfvars` or the `assemble-workloads.sh` script. Remote state data sources can be added later if desired.

- [ ] **Step 1: Write `hub/versions.tf`**

```hcl
terraform {
  required_version = ">= 1.5, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.50"
    }
  }
}
```

- [ ] **Step 2: Write `hub/providers.tf`**

```hcl
# Provider Configuration — Hub Root
# Databricks workspace provider for Unity Catalog and job management.
# AWS provider (security account) for IAM role creation.

provider "databricks" {
  host  = var.databricks_workspace_url
  token = var.databricks_pat
}

# AWS provider targets the security account for IAM role management.
# The hub root owns the IAM roles because it has access to Databricks-assigned
# external IDs at plan time (eliminating the two-pass pattern).
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "security-lakehouse"
      Environment = "poc"
      ManagedBy   = "terraform"
    }
  }
}
```

- [ ] **Step 3: Write `hub/variables.tf`**

```hcl
# Variables — Hub Root

variable "aws_region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "databricks_workspace_url" {
  description = "Databricks workspace URL."
  type        = string
}

variable "databricks_pat" {
  description = "Databricks personal access token."
  type        = string
  sensitive   = true
}

variable "security_account_id" {
  description = "AWS account ID of the security/management account."
  type        = string
}

variable "organization_id" {
  description = "AWS Organizations ID."
  type        = string
}

# Foundation outputs — passed via terraform.tfvars or remote state.
variable "managed_storage_bucket_name" {
  description = "Managed storage S3 bucket name (from foundation root)."
  type        = string
}

variable "managed_storage_bucket_arn" {
  description = "Managed storage S3 bucket ARN (from foundation root)."
  type        = string
}

variable "sns_topic_arn" {
  description = "SNS topic ARN for alert forwarding (from foundation root)."
  type        = string
}

variable "sns_publisher_access_key_id" {
  description = "SNS publisher access key ID (from foundation root)."
  type        = string
}

variable "sns_publisher_secret_access_key" {
  description = "SNS publisher secret access key (from foundation root)."
  type        = string
  sensitive   = true
}

# Workload manifests — populated by assemble-workloads.sh.
variable "workloads" {
  description = "List of workload manifests from assemble-workloads.sh."
  type = list(object({
    cloud      = string
    account_id = string
    alias      = string
    region     = string
    storage = object({
      type        = string
      bucket_name = string
      bucket_arn  = string
    })
    read_only_role_arn = string
    encryption = object({
      type    = string
      key_arn = string
    })
    data_products = map(object({
      format      = string
      path_prefix = string
    }))
  }))
  default = []
}

variable "catalog_name" {
  description = "Unity Catalog catalog name."
  type        = string
  default     = "security_poc"
}

variable "databricks_uc_master_role_arn" {
  description = "Databricks Unity Catalog master role ARN (Databricks-owned account)."
  type        = string
  default     = "arn:aws:iam::<DATABRICKS_AWS_ACCOUNT_ID>:role/unity-catalog-prod-UCMasterRole-<SUFFIX>"
}
```

- [ ] **Step 4: Write `hub/main.tf`**

```hcl
# Hub Root — Databricks Integration Layer
# Creates IAM roles, Databricks storage credentials, external locations,
# Unity Catalog, workspace config, and scheduled jobs.
#
# This root has both AWS and Databricks providers. The AWS provider creates
# IAM roles in the security account. The Databricks provider manages all
# workspace resources.
#
# Dependencies:
#   - Foundation root must be applied (S3 bucket, SNS topic exist)
#   - Workload roots must be applied (assemble-workloads.sh collects outputs)

# ═══════════════════════════════════════════════════════════════════════════════
# IAM Roles — see hub/iam.tf (Task 3)
# ═══════════════════════════════════════════════════════════════════════════════
# IAM roles are defined inline in iam.tf using deterministic ARNs to break
# the circular dependency with cloud_integration. See Task 3 for details.

# ═══════════════════════════════════════════════════════════════════════════════
# Cloud Integration (storage credentials + external locations)
# ═══════════════════════════════════════════════════════════════════════════════

module "cloud_integration" {
  source = "../modules/databricks/cloud-integration"

  # Use deterministic ARNs (from locals in iam.tf) — NOT aws_iam_role outputs.
  # This breaks the circular dependency: cloud_integration has no dependency
  # on the IAM role resources, so Terraform creates credentials first.
  hub_role_arn                = local.hub_role_arn
  managed_storage_role_arn    = local.managed_storage_role_arn
  managed_storage_bucket_name = var.managed_storage_bucket_name
  workloads                   = var.workloads
}

# ═══════════════════════════════════════════════════════════════════════════════
# Unity Catalog
# ═══════════════════════════════════════════════════════════════════════════════

module "unity_catalog" {
  source = "../modules/databricks/unity-catalog"

  catalog_name                = var.catalog_name
  managed_storage_bucket_name = var.managed_storage_bucket_name
  extra_schemas               = ["security"]
}

# ═══════════════════════════════════════════════════════════════════════════════
# Workspace Configuration
# ═══════════════════════════════════════════════════════════════════════════════

module "workspace_config" {
  source = "../modules/databricks/workspace-config"

  catalog_name         = var.catalog_name
  enable_cluster       = false
  enable_sql_warehouse = false
  git_repo_url         = ""
}

# ═══════════════════════════════════════════════════════════════════════════════
# Scheduled Jobs
# ═══════════════════════════════════════════════════════════════════════════════

module "jobs" {
  source = "../modules/databricks/jobs"

  catalog_name                = var.catalog_name
  managed_storage_bucket_name = var.managed_storage_bucket_name

  # Workload bucket names — extract from workload manifests.
  # The jobs module still expects per-workload bucket names (not refactored
  # to for_each yet — jobs reference specific S3 paths per data source).
  workload_a_security_logs_bucket_name = try(
    [for w in var.workloads : w.storage.bucket_name if w.alias == "workload-a"][0],
    ""
  )
  workload_b_security_logs_bucket_name = try(
    [for w in var.workloads : w.storage.bucket_name if w.alias == "workload-b"][0],
    ""
  )

  # SNS forwarding credentials from foundation root.
  sns_topic_arn                   = var.sns_topic_arn
  sns_publisher_access_key_id     = var.sns_publisher_access_key_id
  sns_publisher_secret_access_key = var.sns_publisher_secret_access_key
  aws_region                      = var.aws_region

  # Notebook paths — relative to hub/ root.
  notebook_source_dir                  = "../notebooks/bronze/aws"
  silver_notebook_source_dir           = "../notebooks/silver"
  gold_notebook_source_dir             = "../notebooks/gold"
  threat_intel_notebook_source_dir     = "../notebooks/security/threat_intel"
}
```

**Note:** IAM roles are defined in `hub/iam.tf` (Task 3), not in a module. The circular dependency is resolved by passing deterministic ARN strings (`local.hub_role_arn`) to `cloud_integration` instead of `aws_iam_role.hub.arn`. See Task 3 for the full explanation.

- [ ] **Step 5: Write `hub/outputs.tf`**

```hcl
# Outputs — Hub Root

output "hub_role_arn" {
  value = aws_iam_role.hub.arn
}

output "managed_storage_role_arn" {
  value = aws_iam_role.managed_storage.arn
}

output "hub_credential_external_id" {
  value = module.cloud_integration.hub_credential_external_id
}

output "managed_credential_external_id" {
  value = module.cloud_integration.managed_credential_external_id
}

output "catalog_name" {
  value = module.unity_catalog.catalog_name
}

output "cloudtrail_job_id" {
  value = module.jobs.cloudtrail_job_id
}

output "vpc_flow_job_id" {
  value = module.jobs.vpc_flow_job_id
}

output "guardduty_job_id" {
  value = module.jobs.guardduty_job_id
}

output "config_job_id" {
  value = module.jobs.config_job_id
}

output "threat_intel_pipeline_job_id" {
  value = module.jobs.threat_intel_pipeline_job_id
}
```

- [ ] **Step 6: Write `hub/backend.tf.example`**

```hcl
terraform {
  backend "s3" {
    bucket         = "security-lakehouse-tfstate-<SECURITY_ACCOUNT_ID>"
    key            = "hub/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "security-lakehouse-tflock"
    encrypt        = true
  }
}
```

- [ ] **Step 7: Format and validate**

```bash
cd hub
terraform fmt -check -recursive .
terraform init -backend=false
terraform validate
```

Note: The deterministic ARN pattern in `hub/iam.tf` prevents any circular dependency. If validation fails, check that `local.hub_role_arn` (not `aws_iam_role.hub.arn`) is passed to `cloud_integration`.

- [ ] **Step 8: Commit**

```bash
git add hub/
git commit -m "feat: create hub root (IAM, Databricks integration, Unity Catalog, jobs)"
```

---

### Task 10: Update Jobs Module for New Notebook Paths

**Files:**
- Modify: `modules/databricks/jobs/main.tf`
- Modify: `modules/databricks/jobs/variables.tf`

- [ ] **Step 1: Read current jobs module**

Read `modules/databricks/jobs/main.tf` and `modules/databricks/jobs/variables.tf` to identify all notebook path references.

- [ ] **Step 2: Update `variables.tf` defaults**

Update the default values for notebook source directories:

```hcl
# Old defaults:
#   notebook_source_dir = "../../notebooks/bronze"
#   silver_notebook_source_dir = "../../notebooks/silver"
#   gold_notebook_source_dir = "../../notebooks/gold"
#   threat_intel_notebook_source_dir = "../../notebooks/threat_intel"
# New defaults (relative to hub/ root):
#   notebook_source_dir = "../notebooks/bronze/aws"
#   silver_notebook_source_dir = "../notebooks/silver"
#   gold_notebook_source_dir = "../notebooks/gold"
#   threat_intel_notebook_source_dir = "../notebooks/security/threat_intel"
```

- [ ] **Step 3: Update `main.tf` notebook source references**

Search for all `source` attributes in `databricks_notebook` resources and update filenames:

```
# Bronze notebooks:
#   01_bronze_cloudtrail.py → 01_cloudtrail.py
#   02_bronze_vpc_flow.py → 02_vpc_flow.py
#   03_bronze_guardduty.py → 03_guardduty.py
#   04_bronze_config.py → 04_config.py
#   00_ocsf_common.py → 00_ocsf_common.py (unchanged)

# Threat intel notebooks:
#   01_bronze_threat_intel_ingest.py → 01_bronze_ingest.py
#   02_silver_threat_intel_network.py → 02_silver_network.py
#   00_threat_intel_common.py → 00_threat_intel_common.py (unchanged)
```

- [ ] **Step 4: Update workspace notebook paths**

Update the default `workspace_notebook_path` values:

```
# Old: /Shared/security-lakehouse/bronze
# New: /Shared/security-lakehouse/bronze/aws
# (Silver, gold remain the same)
# Old: /Shared/security-lakehouse/threat_intel
# New: /Shared/security-lakehouse/security/threat_intel
```

- [ ] **Step 5: Format and validate**

```bash
cd modules/databricks/jobs
terraform fmt .
terraform init -backend=false
terraform validate
```

- [ ] **Step 6: Commit**

```bash
git add modules/databricks/jobs/
git commit -m "refactor: update jobs module for reorganized notebook paths"
```

---

### Task 11: Create Scripts

**Files:**
- Create: `scripts/assemble-workloads.sh`
- Create: `scripts/apply-all.sh`
- Create: `scripts/migrate-state.sh`

- [ ] **Step 1: Write `scripts/assemble-workloads.sh`**

```bash
#!/usr/bin/env bash
# =============================================================================
# assemble-workloads.sh — Collect workload outputs into hub tfvars
# =============================================================================
# Iterates over all workload roots, runs `terraform output -json workload_manifest`
# in each, and assembles them into hub/workloads.auto.tfvars.json.
#
# Usage:
#   ./scripts/assemble-workloads.sh
#
# Prerequisites:
#   - All workload roots must be initialized and applied
#   - jq must be installed
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
HUB_DIR="$REPO_ROOT/hub"

if ! command -v jq &>/dev/null; then
  echo "ERROR: jq is required but not installed."
  exit 1
fi

workload_json="[]"
count=0

for dir in "$REPO_ROOT"/workloads/aws-workload-* \
           "$REPO_ROOT"/workloads/azure-workload-* \
           "$REPO_ROOT"/workloads/gcp-workload-*; do
  [[ -d "$dir" ]] || continue

  alias_name=$(basename "$dir")
  echo "  Collecting: $alias_name"

  output=$(cd "$dir" && terraform output -json workload_manifest 2>/dev/null) || {
    echo "  WARNING: Failed to read output from $alias_name (not applied?). Skipping."
    continue
  }

  workload_json=$(echo "$workload_json" | jq --argjson w "$output" '. + [$w]')
  count=$((count + 1))
done

if [[ "$count" -eq 0 ]]; then
  echo "ERROR: No workload outputs found. Apply at least one workload root first."
  exit 1
fi

jq -n --argjson w "$workload_json" '{"workloads": $w}' \
  > "$HUB_DIR/workloads.auto.tfvars.json"

echo ""
echo "Wrote $count workload(s) to hub/workloads.auto.tfvars.json"
```

- [ ] **Step 2: Write `scripts/apply-all.sh`**

```bash
#!/usr/bin/env bash
# =============================================================================
# apply-all.sh — Full deployment sequence for all roots
# =============================================================================
# Applies all 4 roots in dependency order. Bootstrap is skipped if already
# applied (checks for existing state bucket).
#
# Usage:
#   ./scripts/apply-all.sh              # Interactive (prompts before each apply)
#   ./scripts/apply-all.sh --auto       # Auto-approve all applies (CI/CD)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
APPROVE_FLAG=""

if [[ "${1:-}" == "--auto" ]]; then
  APPROVE_FLAG="-auto-approve"
fi

echo "============================================================"
echo "  Security Data Lakehouse — Full Deploy"
echo "============================================================"
echo ""

# ── Step 1: Bootstrap ──────────────────────────────────────────────────────
echo "=== Step 1/4: Bootstrap ==="
(cd "$REPO_ROOT/bootstrap" && terraform init && terraform apply $APPROVE_FLAG)
echo ""

# ── Step 2: Foundation ─────────────────────────────────────────────────────
echo "=== Step 2/4: Foundation (aws-security) ==="
(cd "$REPO_ROOT/foundations/aws-security" && terraform init && terraform apply $APPROVE_FLAG)
echo ""

# ── Step 3: Workloads (parallel) ───────────────────────────────────────────
echo "=== Step 3/4: Workloads ==="
pids=()
dirs=()
for dir in "$REPO_ROOT"/workloads/aws-workload-* \
           "$REPO_ROOT"/workloads/azure-workload-*; do
  [[ -d "$dir" ]] || continue
  [[ -f "$dir/main.tf" ]] || continue
  echo "  Starting: $(basename "$dir")"
  (cd "$dir" && terraform init && terraform apply $APPROVE_FLAG) &
  pids+=($!)
  dirs+=("$dir")
done

failed=0
for i in "${!pids[@]}"; do
  if ! wait "${pids[$i]}"; then
    echo "  ERROR: $(basename "${dirs[$i]}") apply failed."
    failed=1
  fi
done

if [[ "$failed" -eq 1 ]]; then
  echo "ERROR: One or more workload applies failed. Fix and re-run."
  exit 1
fi
echo ""

# ── Step 4: Assemble + Hub ─────────────────────────────────────────────────
echo "=== Step 4/4: Assemble workloads + Hub ==="
"$SCRIPT_DIR/assemble-workloads.sh"
(cd "$REPO_ROOT/hub" && terraform init && terraform apply $APPROVE_FLAG)
echo ""

echo "============================================================"
echo "  All roots applied successfully."
echo "============================================================"
```

- [ ] **Step 3: Write `scripts/migrate-state.sh`**

```bash
#!/usr/bin/env bash
# =============================================================================
# migrate-state.sh — One-time state migration from environments/poc/
# =============================================================================
# Moves resources from the monolithic state into per-root states.
# Run with --dry-run first to preview the migration plan.
#
# Usage:
#   ./scripts/migrate-state.sh --dry-run    # Preview only
#   ./scripts/migrate-state.sh              # Execute migration
#
# Prerequisites:
#   - All new roots must be initialized with backend configured
#   - environments/poc/ state must be accessible
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DRY_RUN=false
BACKUP_DIR="$REPO_ROOT/.state-migration-backup"

if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
  echo "=== DRY RUN MODE — no changes will be made ==="
  echo ""
fi

# ── Backup ─────────────────────────────────────────────────────────────────
if [[ "$DRY_RUN" == false ]]; then
  mkdir -p "$BACKUP_DIR"
  echo "Backing up current state..."
  (cd "$REPO_ROOT/environments/poc" && terraform state pull > "$BACKUP_DIR/poc-$(date +%s).tfstate")
  echo "  Backup saved to $BACKUP_DIR/"
  echo ""
fi

# ── Migration mapping ──────────────────────────────────────────────────────
# Format: "source_address|target_root|target_address"
MIGRATIONS=(
  # Foundation root — S3 and KMS (note: module address changes)
  "module.security_account_baseline.aws_s3_bucket.managed_storage|foundations/aws-security|module.security_foundation.aws_s3_bucket.managed_storage"
  "module.security_account_baseline.aws_s3_bucket_versioning.managed_storage|foundations/aws-security|module.security_foundation.aws_s3_bucket_versioning.managed_storage"
  "module.security_account_baseline.aws_s3_bucket_server_side_encryption_configuration.managed_storage|foundations/aws-security|module.security_foundation.aws_s3_bucket_server_side_encryption_configuration.managed_storage"
  "module.security_account_baseline.aws_s3_bucket_public_access_block.managed_storage|foundations/aws-security|module.security_foundation.aws_s3_bucket_public_access_block.managed_storage"
  "module.security_account_baseline.aws_s3_bucket_policy.managed_storage|foundations/aws-security|module.security_foundation.aws_s3_bucket_policy.managed_storage"

  # Foundation root — SNS (absorbed from sns-alerts)
  "module.sns_alerts.aws_sns_topic.alerts|foundations/aws-security|module.security_foundation.aws_sns_topic.alerts"
  "module.sns_alerts.aws_sns_topic_policy.alerts|foundations/aws-security|module.security_foundation.aws_sns_topic_policy.alerts"
  "module.sns_alerts.aws_iam_user.sns_publisher|foundations/aws-security|module.security_foundation.aws_iam_user.sns_publisher"
  "module.sns_alerts.aws_iam_user_policy.sns_publish|foundations/aws-security|module.security_foundation.aws_iam_user_policy.sns_publish"
  "module.sns_alerts.aws_iam_access_key.sns_publisher|foundations/aws-security|module.security_foundation.aws_iam_access_key.sns_publisher"

  # Workload A root
  "module.workload_a_baseline|workloads/aws-workload-a|module.baseline"
  "module.workload_a_data_sources|workloads/aws-workload-a|module.data_sources"

  # Workload B root
  "module.workload_b_baseline|workloads/aws-workload-b|module.baseline"
  "module.workload_b_data_sources|workloads/aws-workload-b|module.data_sources"

  # Hub root — IAM roles (inline resources, not in a module)
  "module.security_account_baseline.aws_iam_role.managed_storage|hub|aws_iam_role.managed_storage"
  "module.security_account_baseline.aws_iam_role.hub|hub|aws_iam_role.hub"

  # Hub root — Databricks resources
  "module.cloud_integration|hub|module.cloud_integration"
  "module.unity_catalog|hub|module.unity_catalog"
  "module.workspace_config|hub|module.workspace_config"
  "module.bronze_ingestion|hub|module.jobs"
)

echo "Migration plan: ${#MIGRATIONS[@]} resource moves"
echo ""

for entry in "${MIGRATIONS[@]}"; do
  IFS='|' read -r source target_root target_addr <<< "$entry"
  echo "  $source"
  echo "    → $target_root :: $target_addr"

  if [[ "$DRY_RUN" == false ]]; then
    # Pull from source, push to target
    (cd "$REPO_ROOT/environments/poc" && terraform state mv \
      -state-out="$REPO_ROOT/$target_root/terraform.tfstate" \
      "$source" "$target_addr") || {
      echo "    ERROR: Failed to move $source"
      echo "    Rolling back is possible via: terraform state push $BACKUP_DIR/poc-*.tfstate"
      exit 1
    }
  fi
done

echo ""

# ── for_each address changes (cloud-integration external locations) ────────
echo "Renaming for_each-keyed resources in hub state..."
RENAMES=(
  "module.cloud_integration.databricks_external_location.workload_a|module.cloud_integration.databricks_external_location.workload[\"workload-a\"]"
  "module.cloud_integration.databricks_external_location.workload_b|module.cloud_integration.databricks_external_location.workload[\"workload-b\"]"
)

for entry in "${RENAMES[@]}"; do
  IFS='|' read -r old_addr new_addr <<< "$entry"
  echo "  $old_addr → $new_addr"

  if [[ "$DRY_RUN" == false ]]; then
    (cd "$REPO_ROOT/hub" && terraform state mv "$old_addr" "$new_addr") || {
      echo "    ERROR: Failed to rename $old_addr"
      exit 1
    }
  fi
done

echo ""

# ── Validation ─────────────────────────────────────────────────────────────
if [[ "$DRY_RUN" == false ]]; then
  echo "Validating: terraform plan in each root (should show 0 changes)..."
  for root in "foundations/aws-security" "workloads/aws-workload-a" "workloads/aws-workload-b" "hub"; do
    echo "  Checking $root..."
    (cd "$REPO_ROOT/$root" && terraform plan -detailed-exitcode) || {
      echo "  WARNING: $root shows drift. Review before proceeding."
    }
  done
fi

echo ""
echo "State migration complete."
echo "Backup is at: $BACKUP_DIR/"
echo "To rollback: cd environments/poc && terraform state push $BACKUP_DIR/poc-*.tfstate"
```

- [ ] **Step 4: Make scripts executable**

```bash
chmod +x scripts/assemble-workloads.sh scripts/apply-all.sh scripts/migrate-state.sh
```

- [ ] **Step 5: Validate scripts with bash -n**

```bash
bash -n scripts/assemble-workloads.sh
bash -n scripts/apply-all.sh
bash -n scripts/migrate-state.sh
```

Expected: No syntax errors.

- [ ] **Step 6: Commit**

```bash
git add scripts/
git commit -m "feat: add assemble-workloads, apply-all, and migrate-state scripts"
```

---

### Task 12: Validate All Roots

Run `terraform fmt` and `terraform validate` in every root and module to catch issues.

**Files:** None (validation only)

- [ ] **Step 1: Format check across entire repo**

```bash
terraform fmt -check -recursive .
```

Fix any formatting issues found.

- [ ] **Step 2: Validate each new module**

```bash
for mod in modules/aws/security-foundation; do
  echo "=== $mod ==="
  (cd "$mod" && terraform init -backend=false && terraform validate)
done
```

- [ ] **Step 3: Validate each root (backend=false for offline validation)**

```bash
for root in foundations/aws-security workloads/_template-aws workloads/aws-workload-a workloads/aws-workload-b hub; do
  echo "=== $root ==="
  (cd "$root" && terraform init -backend=false && terraform validate)
done
```

Expected: All pass. If any fail, fix the issue and re-validate.

- [ ] **Step 4: Verify no broken cross-references**

```bash
# Check that no TF files reference old module names in new roots
grep -r "module.security_account_baseline" foundations/ hub/ workloads/ --include="*.tf" || echo "Clean"
grep -r "module.sns_alerts" hub/ workloads/ --include="*.tf" || echo "Clean"
grep -r "module.bronze_ingestion" hub/ --include="*.tf" || echo "Clean (should be module.jobs)"
```

- [ ] **Step 5: Commit any fixes**

```bash
git add -A
git commit -m "fix: resolve validation issues across all roots"
```

---

### Task 13: Documentation and Cleanup

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`
- Modify: `.gitignore`
- Modify: `publish-to-public.sh`

- [ ] **Step 1: Update `.gitignore`**

Add entries for new generated files:

```
# Generated by assemble-workloads.sh — contains workload outputs
hub/workloads.auto.tfvars.json

# State migration backups
.state-migration-backup/
```

- [ ] **Step 2: Update `publish-to-public.sh`**

Add exclusions for new paths that should not be synced:

```bash
# Add to rsync excludes:
--exclude='hub/workloads.auto.tfvars.json' \
--exclude='.state-migration-backup' \
```

- [ ] **Step 3: Update CLAUDE.md**

Update the following sections:
- **Architecture Overview** — describe multi-root structure
- **Development Commands** — replace staged apply with root-based apply
- **Module Structure** — add security-foundation module and inline IAM resources in hub/iam.tf
- **Staged Apply Sequence** — replace with the 4-root sequence
- **Adding Workload Accounts** — describe template-copy workflow
- **Notebook Conventions** — update paths (bronze/aws/, security/threat_intel/)

- [ ] **Step 4: Update README.md**

Update:
- Architecture diagram to reflect multi-root structure
- Getting Started section with new apply sequence
- Project Structure tree with new directories
- Adding Workload Accounts section

- [ ] **Step 5: Add deprecation notice to `environments/poc/`**

Create `environments/poc/DEPRECATED.md`:

```markdown
# DEPRECATED

This directory has been superseded by the multi-root architecture:

- `foundations/aws-security/` — Security account foundation (S3, KMS, SNS)
- `workloads/aws-workload-*/` — Per-workload roots (VPC, EC2, data sources)
- `hub/` — Databricks integration (IAM roles, Unity Catalog, jobs)

See the project README for the new apply sequence.

This directory is retained for reference during state migration.
After migration is validated, it can be removed.
```

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "docs: update CLAUDE.md, README, and gitignore for multi-root structure"
```

---

## Implementation Notes

### Circular Dependency Resolution (Task 3 + Task 9)

Resolved by using **deterministic ARN strings** instead of resource attribute references. The hub role names are fixed (`security-lakehouse-hub`, `security-lakehouse-managed-storage`), so the ARNs can be computed as `arn:aws:iam::<account_id>:role/<name>` without creating the roles first. The `cloud_integration` module receives these string literals, breaking the dependency cycle. Terraform creates storage credentials first (no dependency on IAM roles), then creates IAM roles with the correct trust policies (referencing storage credential external IDs). All in a single apply.

This is the same pattern already used by the foundation S3 bucket policy, which references role ARNs before the roles exist.

### Deterministic ARN Pattern for Workload Trust Policies (Task 8)

Workload roots need the hub role ARN for the read-only role trust policy. Since the hub creates IAM roles in Step 4 (after workloads in Step 3), workloads use the same deterministic ARN pattern: `arn:aws:iam::<security_account_id>:role/security-lakehouse-hub`. AWS IAM trust policies don't validate that principals exist at policy creation time, so this is safe.

### Known Limitation: Jobs Module Hardcodes Two Workloads

The jobs module still expects `workload_a_security_logs_bucket_name` and `workload_b_security_logs_bucket_name` as separate variables. Adding a third workload requires modifying the jobs module and hub main.tf. This is intentional for Phase 1 — refactoring the jobs module for dynamic workloads is deferred to when the first non-AWS workload is onboarded (Phase 2), as the notebook parameterization also needs to change at that point.

### State Migration

The `migrate-state.sh` script should be tested against a backup of the real state before running on production. The recommended sequence:

1. `terraform state pull > backup.tfstate` in `environments/poc/`
2. Run `./scripts/migrate-state.sh --dry-run` to preview
3. Run `./scripts/migrate-state.sh` to execute
4. Run `terraform plan` in each new root (should show 0 changes)
5. If any root shows drift, investigate before proceeding
6. After validation, the `environments/poc/` directory can be archived

### Notebook Table References

After reorganizing notebooks, the threat intel tables need to be updated from `bronze.threat_intel_raw` / `silver.threat_intel_network` to `security.threat_intel_raw` / `security.threat_intel_network`. This is a search-and-replace within the notebook Python files. Do this as part of Task 6 (notebook reorganization).
