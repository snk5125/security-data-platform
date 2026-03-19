# Onboarding New AWS Workload Accounts

This guide walks through adding a new AWS workload account to the security lakehouse. The process is repeatable -- each workload account uses its own independent Terraform root created from a template, and the hub automatically discovers new workloads via `assemble-workloads.sh`.

**Time estimate:** 30-45 minutes of Terraform work + ~30 minutes waiting for initial data to flow.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Architecture Context](#2-architecture-context)
3. [Automated Onboarding (Recommended)](#3-automated-onboarding-recommended)
4. [Manual Step-by-Step: Add Workload C](#4-manual-step-by-step-add-workload-c)
5. [Apply Sequence](#5-apply-sequence)
6. [Validation Checklist](#6-validation-checklist)
7. [Why No Security Account Changes Are Needed](#7-why-no-security-account-changes-are-needed)
8. [Scaling Notes](#8-scaling-notes)
9. [Troubleshooting](#9-troubleshooting)
10. [Quick Reference: Files Changed per New Account](#10-quick-reference-files-changed-per-new-account)

---

## 1. Prerequisites

Before starting, confirm:

| Requirement | Detail |
|---|---|
| **AWS Organizations member account** | The new account must be a member of the same AWS Organization. The `OrganizationAccountAccessRole` must exist in the account (auto-created for accounts provisioned via Organizations). |
| **Account ID** | You need the 12-digit AWS account ID for the new workload account. |
| **Security account ID** | You need the 12-digit AWS account ID for the security/management account (for the S3 state backend and hub role ARN). |
| **No existing resources** | The modules create VPCs, EC2 instances, S3 buckets, CloudTrail, GuardDuty, Config, and IAM roles. Ensure no naming conflicts (e.g., existing `default` Config recorder). |
| **Terraform access** | Caller credentials must be able to assume `OrganizationAccountAccessRole` in the new account from the security account. |
| **Unique VPC CIDR** | Choose a `/16` CIDR that doesn't overlap with existing workloads (see CIDR table below). |

### CIDR Allocation Table

| Account | VPC CIDR | Subnet CIDR |
|---|---|---|
| Workload A | `10.0.0.0/16` | `10.0.1.0/24` |
| Workload B | `10.1.0.0/16` | `10.1.1.0/24` |
| Workload C (new) | `10.2.0.0/16` | `10.2.1.0/24` |
| Workload D (future) | `10.3.0.0/16` | `10.3.1.0/24` |

---

## 2. Architecture Context

Each workload account follows this pattern:

```
+---------------------------------------------------------+
|  Workload Account (new)                                 |
|                                                         |
|  VPC + Subnet + IGW + Route Table + Security Group      |
|  +-- Linux EC2 (Amazon Linux 2023, t2.micro)            |
|  +-- Windows EC2 (Windows Server 2022, t2.micro)        |
|                                                         |
|  Security Data Sources:                                 |
|  +-- CloudTrail -> S3 (JSON.gz)                         |
|  +-- VPC Flow Logs -> S3 (text.gz)                      |
|  +-- GuardDuty -> S3 (JSONL.gz, KMS encrypted)          |
|  +-- AWS Config -> S3 (JSON.gz)                         |
|                                                         |
|  S3: lakehouse-workload-c-security-logs-{acct_id}       |
|  KMS: dedicated key for GuardDuty encryption            |
|  IAM: read-only role (trusts hub role)                  |
+---------------------------------------------------------+
         |
         |  Hub role reads S3 directly (cross-account)
         |  Hub role decrypts KMS (cross-account)
         v
+---------------------------------------------------------+
|  Security Account                                       |
|  Hub IAM role <- Databricks storage credential          |
+---------------------------------------------------------+
         |
         v
+---------------------------------------------------------+
|  Databricks (Unity Catalog)                             |
|  External location -> Auto Loader -> Bronze tables      |
+---------------------------------------------------------+
```

**Multi-root architecture:** Each workload has its own independent Terraform root under `workloads/aws-<alias>/`. The hub root discovers workloads via `assemble-workloads.sh`, which collects `workload_manifest` outputs into `hub/workloads.auto.tfvars.json`.

**IAM access chain:** Databricks UC master role -> hub storage credential -> hub IAM role -> direct S3 read + KMS decrypt (cross-account, authorized by bucket policy + KMS key policy in the workload account).

---

## 3. Automated Onboarding (Recommended)

Use the `onboard_workload_account.sh` script to automate all file modifications:

```bash
./onboard_workload_account.sh \
  --alias workload-c \
  --account-id 123456789012 \
  --security-account-id <SECURITY_ACCOUNT_ID> \
  --vpc-cidr 10.2.0.0/16 \
  --subnet-cidr 10.2.1.0/24
```

This creates the workload root from the template, generates `terraform.tfvars` and `backend.tf`, updates the jobs module and notebooks. See `onboard_workload_account_usage.md` for full details.

After running the script, skip to [Section 5: Apply Sequence](#5-apply-sequence).

---

## 4. Manual Step-by-Step: Add Workload C

All file paths are relative to the repository root.

### Step 4.1 -- Copy the workload template

```bash
cp -r workloads/_template-aws workloads/aws-workload-c
```

### Step 4.2 -- Create terraform.tfvars

**File:** `workloads/aws-workload-c/terraform.tfvars`

```hcl
aws_region          = "us-east-1"
account_alias       = "workload-c"
account_id          = "XXXXXXXXXXXX"     # <- 12-digit workload account ID
vpc_cidr            = "10.2.0.0/16"
public_subnet_cidr  = "10.2.1.0/24"
security_account_id = "XXXXXXXXXXXX"     # <- 12-digit security account ID
```

### Step 4.3 -- Create backend.tf

**File:** `workloads/aws-workload-c/backend.tf`

```hcl
terraform {
  backend "s3" {
    bucket         = "security-lakehouse-tfstate-XXXXXXXXXXXX"   # <- security account ID
    key            = "workloads/workload-c/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "security-lakehouse-tflock"
    encrypt        = true
  }
}
```

### Step 4.4 -- Add bucket variable to jobs module

**File:** `modules/databricks/jobs/variables.tf`

Add after the existing `workload_b_security_logs_bucket_name` variable:

```hcl
variable "workload_c_security_logs_bucket_name" {
  description = "Workload C security logs S3 bucket name -- source for Auto Loader"
  type        = string
}
```

### Step 4.5 -- Update common_params in jobs module

**File:** `modules/databricks/jobs/main.tf`

Add the new bucket to the `common_params` locals block:

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

### Step 4.6 -- Wire bucket into hub/main.tf jobs module block

**File:** `hub/main.tf`

Add a new `try()` block to extract the bucket name from workload manifests:

```hcl
module "jobs" {
  source = "../modules/databricks/jobs"

  # ... existing config ...

  workload_a_security_logs_bucket_name = try(
    [for w in var.workloads : w.storage.bucket_name if w.alias == "workload-a"][0],
    ""
  )
  workload_b_security_logs_bucket_name = try(
    [for w in var.workloads : w.storage.bucket_name if w.alias == "workload-b"][0],
    ""
  )
  workload_c_security_logs_bucket_name = try(                                        # NEW
    [for w in var.workloads : w.storage.bucket_name if w.alias == "workload-c"][0],
    ""
  )

  # ... rest of config ...
}
```

### Step 4.7 -- Update Auto Loader notebooks to read from Workload C

**Files:** All four notebooks in `notebooks/bronze/aws/`

Each notebook has a `source_paths` dictionary that lists the workload buckets. Add the new workload to each. For example, in `notebooks/bronze/aws/01_cloudtrail.py`:

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
- `01_cloudtrail.py` -> `/cloudtrail/AWSLogs/`
- `02_vpc_flow.py` -> `/vpc-flow-logs/AWSLogs/`
- `03_guardduty.py` -> `/AWSLogs/`
- `04_config.py` -> `/config/AWSLogs/`

---

## 5. Apply Sequence

The multi-root apply follows this order:

```
Step 1: Initialize and apply the new workload root
        cd workloads/aws-workload-c
        terraform init
        terraform plan    -> Verify ~27 new resources
        terraform apply
        -> Creates VPC, EC2 instances, S3 bucket, CloudTrail, GuardDuty,
           Config, KMS key, IAM roles

Step 2: Assemble workload outputs
        cd ../..
        ./scripts/assemble-workloads.sh
        -> Collects workload_manifest outputs into hub/workloads.auto.tfvars.json

Step 3: Apply the hub
        cd hub
        terraform plan    -> Verify 1 new external location + updated job params
        terraform apply
        -> Creates Databricks external location (via cloud_integration for_each)
        -> Updates job parameters with new bucket name

Step 4: Wait 30 minutes for security data to flow
        -> CloudTrail, VPC Flow Logs start immediately
        -> GuardDuty findings may take hours
        -> Config snapshots arrive within ~30 min
```

**Key difference from the old monolithic structure:** The workload root is fully independent -- it has its own providers, state, and lifecycle. No provider aliases, no cross-root module blocks. The hub discovers workloads dynamically via `assemble-workloads.sh`.

---

## 6. Validation Checklist

After apply, verify each layer:

### AWS Infrastructure
```bash
# Verify VPC exists in new account (use assume-role or profile)
aws ec2 describe-vpcs --filters "Name=tag:Name,Values=lakehouse-workload-c-vpc"

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
aws sts assume-role --role-arn arn:aws:iam::XXXXXXXXXXXX:role/lakehouse-hub-role \
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

## 7. Why No Security Account Changes Are Needed

The hub role's IAM policy uses wildcards that automatically cover new workload accounts:

| Policy Statement | Resource Pattern | Why It Auto-Matches |
|---|---|---|
| `ReadWorkloadSecurityLogs` | `arn:aws:s3:::*-security-logs-*` | New bucket `lakehouse-workload-c-security-logs-{id}` matches the `*-security-logs-*` pattern |
| `DecryptGuardDutyFindings` | `arn:aws:kms:*:*:key/*` | New KMS key in any org account is covered |

The cloud-integration module uses `for_each` over `var.workloads`, so new workloads get external locations automatically -- no manual module changes needed.

The data-sources module handles all workload-side IAM automatically:
- Bucket policy grants the hub role `s3:GetObject` + `s3:ListBucket`
- KMS key policy grants the hub role `kms:Decrypt`
- Read-only role trusts the hub role for `sts:AssumeRole`

These are created from the deterministic hub role ARN passed via `security_account_id` -- no hardcoded ARNs.

---

## 8. Scaling Notes

### Current Limitations

**Jobs module uses per-workload variables.** The jobs module has not yet been refactored to use `for_each` over the workloads list. Each new workload requires a new variable in the jobs module and a corresponding `try()` block in `hub/main.tf`. The cloud-integration module is already dynamic.

**Notebook bucket parameters are positional.** The current notebooks use named widget parameters (`workload_a_bucket`, `workload_b_bucket`, etc.). At ~5+ accounts, consider refactoring notebooks to accept a comma-separated list or JSON array of bucket names instead.

**External locations are per-bucket.** Each workload gets one external location via `for_each`. At scale, consider creating external locations at a higher S3 prefix level.

### Naming Convention

All resources follow the pattern `lakehouse-{account_alias}-{resource_type}`:

| Resource | Naming Pattern | Example |
|---|---|---|
| Workload root | `workloads/aws-{alias}/` | `workloads/aws-workload-c/` |
| VPC | `lakehouse-{alias}-vpc` | `lakehouse-workload-c-vpc` |
| S3 Bucket | `lakehouse-{alias}-security-logs-{acct_id}` | `lakehouse-workload-c-security-logs-999999999999` |
| IAM Role | `lakehouse-{alias}-read-only-role` | `lakehouse-workload-c-read-only-role` |
| CloudTrail | `lakehouse-{alias}-trail` | `lakehouse-workload-c-trail` |
| External Location | `security-logs-{alias}` | `security-logs-workload-c` |

The `account_alias` must be unique and should follow the `workload-{letter}` convention.

### What If You Need to Remove a Workload Account?

1. Remove the workload root directory (`workloads/aws-workload-c/`)
2. Remove the bucket variable from `modules/databricks/jobs/variables.tf`
3. Remove the bucket from `common_params` in `modules/databricks/jobs/main.tf`
4. Remove the `try()` block from `hub/main.tf`
5. Remove the bucket widget and source path from each notebook in `notebooks/bronze/aws/`
6. Re-run `./scripts/assemble-workloads.sh` (removes the workload from the manifest)
7. Run `cd hub && terraform apply` -- Terraform will destroy the external location
8. Run `cd workloads/aws-workload-c && terraform destroy` to remove AWS resources
9. **Caution:** This destroys the S3 bucket and all security logs in it. Consider backing up first.

---

## 9. Troubleshooting

### "InsufficientS3BucketPolicyException" on CloudTrail creation

The bucket policy must exist before CloudTrail. The data-sources module handles this with `depends_on`, but if you see this error, verify the bucket policy was created first:

```bash
aws s3api get-bucket-policy --bucket lakehouse-workload-c-security-logs-XXXXXXXXXXXX
```

### "InsufficientDeliveryPolicyException" on Config delivery channel

Same root cause -- the bucket policy must grant the Config service write access. The module handles this, but the Config recorder -> delivery channel -> recorder status chain is fragile. If it fails, re-run `terraform apply`.

### GuardDuty export not working

GuardDuty S3 export requires:
1. KMS key policy granting `guardduty.amazonaws.com` encrypt access
2. Bucket policy granting `guardduty.amazonaws.com` PutObject access
3. The detector must be active

Check: `aws guardduty list-publishing-destinations --detector-id <id>`

### Databricks external location validation fails

If the external location creation fails with a permissions error:
1. Verify the bucket policy grants the hub role ARN read access
2. Verify the hub role trust policy includes the Databricks external ID
3. Verify self-assume is configured in the hub IAM role trust policy

### "CF_EMPTY_DIR" in Auto Loader notebook

This is normal -- it means no files exist yet at the S3 path. Wait 30 minutes for data to flow. The notebooks handle this gracefully with a try/except.

### `assemble-workloads.sh` skips the new workload

The workload root must be initialized and applied before `assemble-workloads.sh` can collect its outputs. Run `terraform init && terraform apply` in `workloads/aws-<alias>/` first.

---

## 10. Quick Reference: Files Changed per New Account

### New files (created from template)

| File | Contents |
|---|---|
| `workloads/aws-<alias>/` | Full workload root (copied from `_template-aws/`) |
| `workloads/aws-<alias>/terraform.tfvars` | Account-specific values |
| `workloads/aws-<alias>/backend.tf` | S3 backend with unique state key |

### Modified files

| File | Change Type | What to Add |
|---|---|---|
| `modules/databricks/jobs/variables.tf` | Add variable | `<alias>_security_logs_bucket_name` |
| `modules/databricks/jobs/main.tf` | Update locals | Add `<alias>_bucket` to `common_params` |
| `hub/main.tf` | Update module | Add `try()` block for new bucket in jobs module |
| `notebooks/bronze/aws/01_cloudtrail.py` | Update paths | Add widget + `source_paths` entry |
| `notebooks/bronze/aws/02_vpc_flow.py` | Update paths | Add widget + `source_paths` entry |
| `notebooks/bronze/aws/03_guardduty.py` | Update paths | Add widget + `source_paths` entry |
| `notebooks/bronze/aws/04_config.py` | Update paths | Add widget + `source_paths` entry |

### Auto-generated files (no manual changes)

| File | How it's populated |
|---|---|
| `hub/workloads.auto.tfvars.json` | Generated by `assemble-workloads.sh` from workload outputs |

**Total: 1 new workload root + 7 modified files, ~27 new AWS resources, 1 new Databricks external location, 4 updated Databricks jobs.**

**No changes needed:** security account foundation, cloud-integration module (uses `for_each`), unity-catalog module, workspace-config module, hub IAM roles (wildcard patterns).
