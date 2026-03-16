# Onboarding New AWS Workload Accounts

This guide walks through adding a new AWS workload account to the security lakehouse. The process is repeatable — each workload account uses the same two reusable Terraform modules and follows the same wiring pattern.

**Time estimate:** 30–45 minutes of Terraform work + ~30 minutes waiting for initial data to flow.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Architecture Context](#2-architecture-context)
3. [Step-by-Step: Add Workload C](#3-step-by-step-add-workload-c)
4. [Apply Sequence](#4-apply-sequence)
5. [Validation Checklist](#5-validation-checklist)
6. [Why No Security Account Changes Are Needed](#6-why-no-security-account-changes-are-needed)
7. [Scaling Notes](#7-scaling-notes)
8. [Troubleshooting](#8-troubleshooting)
9. [Quick Reference: Files Changed per New Account](#9-quick-reference-files-changed-per-new-account)

---

## 1. Prerequisites

Before starting, confirm:

| Requirement | Detail |
|---|---|
| **AWS Organizations member account** | The new account must be a member of the same AWS Organization (`<ORGANIZATION_ID>`). The `OrganizationAccountAccessRole` must exist in the account (auto-created for accounts provisioned via Organizations). |
| **Account ID** | You need the 12-digit AWS account ID for the new workload account. |
| **No existing resources** | The modules create VPCs, EC2 instances, S3 buckets, CloudTrail, GuardDuty, Config, and IAM roles. Ensure no naming conflicts (e.g., existing `default` Config recorder). |
| **Terraform access** | Caller credentials must be able to assume `OrganizationAccountAccessRole` in the new account from the security account (<SECURITY_ACCOUNT_ID>). |
| **Unique VPC CIDR** | Choose a `/16` CIDR that doesn't overlap with existing workloads (see CIDR table below). |

### CIDR Allocation Table

| Account | VPC CIDR | Subnet CIDR |
|---|---|---|
| Workload A (<WORKLOAD_A_ACCOUNT_ID>) | `10.0.0.0/16` | `10.0.1.0/24` |
| Workload B (<WORKLOAD_B_ACCOUNT_ID>) | `10.1.0.0/16` | `10.1.1.0/24` |
| Workload C (new) | `10.2.0.0/16` | `10.2.1.0/24` |
| Workload D (future) | `10.3.0.0/16` | `10.3.1.0/24` |

---

## 2. Architecture Context

Each workload account follows this pattern:

```
┌─────────────────────────────────────────────────────┐
│  Workload Account (new)                             │
│                                                     │
│  VPC + Subnet + IGW + Route Table + Security Group  │
│  ├── Linux EC2 (Amazon Linux 2023, t2.micro)        │
│  └── Windows EC2 (Windows Server 2022, t2.micro)    │
│                                                     │
│  Security Data Sources:                             │
│  ├── CloudTrail → S3 (JSON.gz)                      │
│  ├── VPC Flow Logs → S3 (text.gz)                   │
│  ├── GuardDuty → S3 (JSONL.gz, KMS encrypted)       │
│  └── AWS Config → S3 (JSON.gz)                      │
│                                                     │
│  S3: lakehouse-workload-c-security-logs-{acct_id}   │
│  KMS: dedicated key for GuardDuty encryption         │
│  IAM: read-only role (trusts hub role)               │
└─────────────────────────────────────────────────────┘
         │
         │  Hub role reads S3 directly (cross-account)
         │  Hub role decrypts KMS (cross-account)
         ▼
┌─────────────────────────────────────────────────────┐
│  Security Account (<SECURITY_ACCOUNT_ID>)                    │
│  Hub IAM role ← Databricks storage credential       │
└─────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────┐
│  Databricks (Unity Catalog)                         │
│  External location → Auto Loader → Bronze tables    │
└─────────────────────────────────────────────────────┘
```

**IAM access chain:** Databricks UC master role → hub storage credential → hub IAM role → direct S3 read + KMS decrypt (cross-account, authorized by bucket policy + KMS key policy in the workload account).

---

## 3. Step-by-Step: Add Workload C

All file paths are relative to the repository root.

### Step 3.1 — Add the account variable

**File:** `environments/poc/variables.tf`

Add after the existing `workload_b_account_id` variable:

```hcl
variable "workload_c_account_id" {
  description = "AWS account ID of workload account C (hosts VPC, EC2, security data sources)"
  type        = string
}
```

### Step 3.2 — Set the account ID value

**File:** `environments/poc/terraform.tfvars`

Add under the AWS Account Topology section:

```hcl
workload_c_account_id = "XXXXXXXXXXXX"   # ← replace with actual 12-digit account ID
```

### Step 3.3 — Add the provider alias

**File:** `environments/poc/providers.tf`

Add after the `aws.workload_b` provider block:

```hcl
# ── Workload account C ────────────────────────────────────────────────────────
# Same pattern as workload A/B — assumes into the member account.
provider "aws" {
  alias  = "workload_c"
  region = var.aws_region

  assume_role {
    role_arn = "arn:aws:iam::${var.workload_c_account_id}:role/OrganizationAccountAccessRole"
  }

  default_tags {
    tags = local.common_tags
  }
}
```

### Step 3.4 — Add workload baseline module (Phase 3)

**File:** `environments/poc/main.tf`

Add after the existing `workload_b_baseline` module block:

```hcl
module "workload_c_baseline" {
  source = "../../modules/aws/workload-account-baseline"

  providers = {
    aws = aws.workload_c
  }

  account_alias      = "workload-c"
  account_id         = var.workload_c_account_id
  vpc_cidr           = "10.2.0.0/16"
  public_subnet_cidr = "10.2.1.0/24"

  tags = local.common_tags
}
```

**What this creates (9 resources):**
- VPC (`lakehouse-workload-c-vpc`)
- Public subnet, internet gateway, route table + association
- Permissive security group (intentional — generates GuardDuty findings)
- TLS + AWS key pair (`lakehouse-workload-c-key`)
- Linux EC2 instance (`lakehouse-workload-c-linux`)
- Windows EC2 instance (`lakehouse-workload-c-windows`)

### Step 3.5 — Add data sources module (Phase 4)

**File:** `environments/poc/main.tf`

Add after the existing `workload_b_data_sources` module block:

```hcl
module "workload_c_data_sources" {
  source = "../../modules/aws/data-sources"

  providers = {
    aws = aws.workload_c
  }

  account_alias = "workload-c"
  account_id    = var.workload_c_account_id
  region        = var.aws_region
  vpc_id        = module.workload_c_baseline.vpc_id
  hub_role_arn  = module.security_account_baseline.hub_role_arn

  tags = local.common_tags
}
```

**What this creates (17 resources):**
- S3 bucket (`lakehouse-workload-c-security-logs-{acct_id}`) with versioning, encryption, public access block
- Bucket policy granting: CloudTrail, VPC Flow Logs, GuardDuty, Config write access + hub role read access
- KMS key for GuardDuty export (key policy grants GuardDuty + hub role)
- Read-only IAM role (`lakehouse-workload-c-read-only-role`) trusting the hub role
- CloudTrail trail (`lakehouse-workload-c-trail`)
- VPC Flow Log attached to the workload VPC
- GuardDuty detector + S3 publishing destination
- Config recorder + delivery channel + recorder status
- Config IAM role with AWS-managed policy

### Step 3.6 — Add external location variable to cloud integration module

**File:** `modules/databricks/cloud-integration/variables.tf`

Add after the existing `workload_b_security_logs_bucket_name` variable:

```hcl
variable "workload_c_security_logs_bucket_name" {
  description = "Name of the security-logs S3 bucket in workload account C (e.g., 'lakehouse-workload-c-security-logs-123456')."
  type        = string
}
```

### Step 3.7 — Add external location resource to cloud integration module

**File:** `modules/databricks/cloud-integration/main.tf`

Add after the existing `databricks_external_location.workload_b` resource (around line 104):

```hcl
# Workload C security logs — same data sources as workload A/B but from the
# third workload account.
resource "databricks_external_location" "workload_c" {
  name            = "workload-c-security-logs"
  url             = "s3://${var.workload_c_security_logs_bucket_name}/"
  credential_name = databricks_storage_credential.hub.name
  comment         = "Security logs from workload account C (CloudTrail, Flow Logs, GuardDuty, Config)"

  read_only = true

  depends_on = [databricks_storage_credential.hub]
}
```

### Step 3.8 — Wire the new bucket into the cloud integration module call

**File:** `environments/poc/main.tf`

Update the existing `cloud_integration` module block to add the new variable:

```hcl
module "cloud_integration" {
  source = "../../modules/databricks/cloud-integration"

  hub_role_arn                = module.security_account_baseline.hub_role_arn
  managed_storage_role_arn    = module.security_account_baseline.managed_storage_role_arn
  managed_storage_bucket_name = module.security_account_baseline.managed_storage_bucket_name

  workload_a_security_logs_bucket_name = module.workload_a_data_sources.security_logs_bucket_name
  workload_b_security_logs_bucket_name = module.workload_b_data_sources.security_logs_bucket_name
  workload_c_security_logs_bucket_name = module.workload_c_data_sources.security_logs_bucket_name   # NEW
}
```

### Step 3.9 — Add bucket variable to bronze ingestion module

**File:** `modules/databricks/jobs/variables.tf`

Add after the existing `workload_b_security_logs_bucket_name` variable:

```hcl
variable "workload_c_security_logs_bucket_name" {
  description = "Workload C security logs S3 bucket name — source for Auto Loader"
  type        = string
}
```

### Step 3.10 — Add Workload C to the Auto Loader notebook parameters

**File:** `modules/databricks/jobs/main.tf`

Update the `locals` block to include the new bucket in `common_params`:

```hcl
locals {
  checkpoint_base = "s3://${var.managed_storage_bucket_name}/checkpoints/bronze"

  common_params = {
    workload_a_bucket = var.workload_a_security_logs_bucket_name
    workload_b_bucket = var.workload_b_security_logs_bucket_name
    workload_c_bucket = var.workload_c_security_logs_bucket_name   # NEW
    checkpoint_base   = local.checkpoint_base
  }
}
```

### Step 3.11 — Wire the new bucket into the bronze ingestion module call

**File:** `environments/poc/main.tf`

Update the existing `bronze_ingestion` module block:

```hcl
module "bronze_ingestion" {
  source = "../../modules/databricks/jobs"

  catalog_name                         = "security_poc"
  managed_storage_bucket_name          = module.security_account_baseline.managed_storage_bucket_name
  workload_a_security_logs_bucket_name = module.workload_a_data_sources.security_logs_bucket_name
  workload_b_security_logs_bucket_name = module.workload_b_data_sources.security_logs_bucket_name
  workload_c_security_logs_bucket_name = module.workload_c_data_sources.security_logs_bucket_name   # NEW
}
```

### Step 3.12 — Update Auto Loader notebooks to read from Workload C

**Files:** All four notebooks in `notebooks/bronze/`

Each notebook has a `source_paths` dictionary that lists the workload buckets. Add the new workload to each. For example, in `notebooks/bronze/01_bronze_cloudtrail.py`:

```python
# Add widget for workload C
dbutils.widgets.text("workload_c_bucket", "", "Workload C Bucket")
workload_c_bucket = dbutils.widgets.get("workload_c_bucket")

# Update source_paths to include workload C
source_paths = {
    "workload_a": f"s3://{workload_a_bucket}/cloudtrail/AWSLogs/",
    "workload_b": f"s3://{workload_b_bucket}/cloudtrail/AWSLogs/",
    "workload_c": f"s3://{workload_c_bucket}/cloudtrail/AWSLogs/",   # NEW
}
```

Repeat for all four notebooks, adjusting the S3 prefix per data source:
- `01_bronze_cloudtrail.py` → `/cloudtrail/AWSLogs/`
- `02_bronze_vpc_flow.py` → `/vpc-flow-logs/AWSLogs/` (check existing path pattern)
- `03_bronze_guardduty.py` → `/guardduty/` (check existing path pattern)
- `04_bronze_config.py` → `/config/AWSLogs/` (check existing path pattern)

---

## 4. Apply Sequence

The Terraform apply must follow this order due to cross-module dependencies:

```
Step 1: terraform plan (full)
        → Verify ~28 new resources, ~4 updated resources, 0 destroyed

Step 2: terraform apply -target=module.workload_c_baseline
        → Creates VPC, EC2 instances (9 resources)
        → Wait for completion

Step 3: terraform apply -target=module.workload_c_data_sources
        → Creates S3, IAM, CloudTrail, GuardDuty, Config (17 resources)
        → Depends on: Step 2 (vpc_id) and security_account_baseline (hub_role_arn)

Step 4: terraform apply
        → Applies remaining changes:
          - cloud_integration: new external location (1 resource)
          - bronze_ingestion: updated job parameters (4 resources modified)
        → Depends on: Step 3 (security_logs_bucket_name)

Step 5: Wait 30 minutes for security data to flow
        → CloudTrail, VPC Flow Logs start immediately
        → GuardDuty findings may take hours
        → Config snapshots arrive within ~30 min
```

**Alternative:** If you're comfortable with Terraform resolving the dependency graph automatically, a single `terraform apply` will work — Terraform will create resources in the correct order based on implicit dependencies. The staged approach above is safer for production environments.

---

## 5. Validation Checklist

After apply, verify each layer:

### AWS Infrastructure
```bash
# Verify VPC exists in new account
aws ec2 describe-vpcs --filters "Name=tag:Name,Values=lakehouse-workload-c-vpc" \
  --profile workload-c  # or use assume-role

# Verify S3 bucket exists and has the correct policy
aws s3api get-bucket-policy --bucket lakehouse-workload-c-security-logs-XXXXXXXXXXXX

# Verify CloudTrail is logging
aws cloudtrail get-trail-status --name lakehouse-workload-c-trail

# Verify GuardDuty detector is active
aws guardduty list-detectors

# Verify Config recorder is recording
aws configservice describe-configuration-recorder-status
```

### Cross-Account Access
```bash
# From security account: verify hub role can read the new bucket
aws sts assume-role --role-arn arn:aws:iam::<SECURITY_ACCOUNT_ID>:role/lakehouse-hub-role \
  --role-session-name test-hub-access
# Then:
aws s3 ls s3://lakehouse-workload-c-security-logs-XXXXXXXXXXXX/
```

### Databricks
```sql
-- Verify external location is accessible
LIST 's3://lakehouse-workload-c-security-logs-XXXXXXXXXXXX/';

-- After data flows, verify bronze tables include new account data
SELECT DISTINCT _source_file
FROM security_poc.bronze.cloudtrail_raw
WHERE _source_file LIKE '%workload-c%';
```

---

## 6. Why No Security Account Changes Are Needed

The hub role's IAM policy uses wildcards that automatically cover new workload accounts:

| Policy Statement | Resource Pattern | Why It Auto-Matches |
|---|---|---|
| `ReadWorkloadSecurityLogs` | `arn:aws:s3:::*-security-logs-*` | New bucket `lakehouse-workload-c-security-logs-{id}` matches the `*-security-logs-*` pattern |
| `DecryptGuardDutyFindings` | `arn:aws:kms:*:*:key/*` | New KMS key in any org account is covered |
| `AssumeWorkloadReadOnlyRoles` | `arn:aws:iam::*:role/lakehouse-read-only` | See note below |

**Note on the AssumeRole pattern:** The hub role's STS policy uses the exact role name `lakehouse-read-only`, but the data-sources module creates roles named `lakehouse-{alias}-read-only-role`. In practice, this doesn't block Databricks because the primary access path uses the hub role's **direct S3 permissions** (granted by the bucket policy), not the chain-assume through read-only roles. If you want the chain-assume path to work as well, update the hub role policy resource to `arn:aws:iam::*:role/lakehouse-*-read-only-role` — but this is not required for Databricks ingestion.

The data-sources module handles all workload-side IAM automatically:
- Bucket policy grants the hub role `s3:GetObject` + `s3:ListBucket`
- KMS key policy grants the hub role `kms:Decrypt`
- Read-only role trusts the hub role for `sts:AssumeRole`

These are created from the `hub_role_arn` variable passed to the module — no hardcoded ARNs.

---

## 7. Scaling Notes

### Current Limitations

**Terraform provider aliases cannot use `for_each`.** Each workload account requires its own provider alias block and two module invocations (baseline + data sources). This is a Terraform language limitation, not a design choice. Adding 10 accounts means 10 provider blocks and 20 module blocks.

**Notebook bucket parameters are positional.** The current notebooks use named widget parameters (`workload_a_bucket`, `workload_b_bucket`, etc.). At ~5+ accounts, consider refactoring notebooks to accept a comma-separated list or JSON array of bucket names instead.

**External locations are per-bucket.** Each workload gets one external location. At scale, consider creating external locations at a higher S3 prefix level or using a single external location with broader scope.

### Naming Convention

All resources follow the pattern `lakehouse-{account_alias}-{resource_type}`:

| Resource | Naming Pattern | Example |
|---|---|---|
| VPC | `lakehouse-{alias}-vpc` | `lakehouse-workload-c-vpc` |
| S3 Bucket | `lakehouse-{alias}-security-logs-{acct_id}` | `lakehouse-workload-c-security-logs-999999999999` |
| IAM Role | `lakehouse-{alias}-read-only-role` | `lakehouse-workload-c-read-only-role` |
| CloudTrail | `lakehouse-{alias}-trail` | `lakehouse-workload-c-trail` |
| External Location | `{alias}-security-logs` | `workload-c-security-logs` |

The `account_alias` must be unique and should follow the `workload-{letter}` convention.

### What If You Need to Remove a Workload Account?

1. Remove the module blocks from `main.tf`
2. Remove the variable from `variables.tf` and `terraform.tfvars`
3. Remove the provider alias from `providers.tf`
4. Remove the external location resource and variable from the cloud-integration module
5. Remove the bucket variable from the jobs module and `common_params`
6. Remove the bucket reference from notebooks
7. Run `terraform apply` — Terraform will destroy the resources in the correct order
8. **Caution:** This destroys the S3 bucket and all security logs in it. Consider backing up first.

---

## 8. Troubleshooting

### "InsufficientS3BucketPolicyException" on CloudTrail creation

The bucket policy must exist before CloudTrail. The data-sources module handles this with `depends_on`, but if you see this error, verify the bucket policy was created first:

```bash
aws s3api get-bucket-policy --bucket lakehouse-workload-c-security-logs-XXXXXXXXXXXX
```

### "InsufficientDeliveryPolicyException" on Config delivery channel

Same root cause — the bucket policy must grant the Config service write access. The module handles this, but the Config recorder → delivery channel → recorder status chain is fragile. If it fails, re-run `terraform apply`.

### GuardDuty export not working

GuardDuty S3 export requires:
1. KMS key policy granting `guardduty.amazonaws.com` encrypt access
2. Bucket policy granting `guardduty.amazonaws.com` PutObject access
3. The detector must be active

Check: `aws guardduty list-publishing-destinations --detector-id <id>`

### Databricks external location validation fails

If the external location creation fails with a permissions error:
1. Verify the bucket policy grants the hub role ARN (not the read-only role) read access
2. Verify the hub role trust policy includes the Databricks external ID
3. Verify `enable_self_assume = true` in `terraform.tfvars`

### "CF_EMPTY_DIR" in Auto Loader notebook

This is normal — it means no files exist yet at the S3 path. Wait 30 minutes for data to flow. The notebooks handle this gracefully with a try/except.

---

## 9. Quick Reference: Files Changed per New Account

| File | Change Type | What to Add |
|---|---|---|
| `environments/poc/variables.tf` | Add variable | `workload_c_account_id` |
| `environments/poc/terraform.tfvars` | Add value | `workload_c_account_id = "..."` |
| `environments/poc/providers.tf` | Add provider block | `aws.workload_c` alias |
| `environments/poc/main.tf` | Add 2 modules | `workload_c_baseline`, `workload_c_data_sources` |
| `environments/poc/main.tf` | Update 2 modules | `cloud_integration`, `bronze_ingestion` (add bucket var) |
| `modules/databricks/cloud-integration/variables.tf` | Add variable | `workload_c_security_logs_bucket_name` |
| `modules/databricks/cloud-integration/main.tf` | Add resource | `databricks_external_location.workload_c` |
| `modules/databricks/jobs/variables.tf` | Add variable | `workload_c_security_logs_bucket_name` |
| `modules/databricks/jobs/main.tf` | Update locals | Add `workload_c_bucket` to `common_params` |
| `notebooks/bronze/*.py` (4 files) | Update paths | Add `workload_c` to `source_paths` dict |

**Total: 10 files modified, ~26 new AWS resources, 1 new Databricks resource, 4 updated Databricks jobs.**

**No changes needed:** security-account-baseline module, workload-account-baseline module, data-sources module, unity-catalog module, workspace-config module.
