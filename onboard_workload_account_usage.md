# Usage Guide: onboard_workload_account.sh

Automates all file modifications needed to add a new AWS workload account to the security lakehouse. The script creates a new workload Terraform root from the template, updates the hub jobs module, and adds the new workload to bronze notebooks. It does **not** run `terraform apply`.

## Prerequisites

Before running the script, ensure:

1. **AWS Organizations membership** -- The new account must be a member of your AWS Organization with the `OrganizationAccountAccessRole` IAM role available (created automatically by AWS Organizations).
2. **CIDR planning** -- Choose a VPC CIDR and subnet CIDR that do not overlap with existing workloads:

   | Account    | VPC CIDR      | Subnet CIDR   |
   |------------|---------------|----------------|
   | workload-a | 10.0.0.0/16   | 10.0.1.0/24   |
   | workload-b | 10.1.0.0/16   | 10.1.1.0/24   |
   | workload-c | 10.2.0.0/16   | 10.2.1.0/24   |
   | workload-d | 10.3.0.0/16   | 10.3.1.0/24   |

3. **Security account ID** -- You need the 12-digit AWS account ID of the security/management account (used for the S3 state backend and deterministic hub role ARN).
4. **macOS or Linux** -- Compatible with macOS bash 3.x and Linux bash 4+.

## Syntax

```bash
./onboard_workload_account.sh \
  --alias <name> \
  --account-id <12-digit> \
  --security-account-id <12-digit> \
  --vpc-cidr <cidr> \
  --subnet-cidr <cidr>
```

### Arguments

| Argument              | Required | Description                                                      | Example          |
|-----------------------|----------|------------------------------------------------------------------|------------------|
| `--alias`             | Yes      | Workload account alias. Lowercase, alphanumeric, hyphens only.   | `workload-c`     |
| `--account-id`        | Yes      | 12-digit AWS account ID of the new workload account.             | `123456789012`   |
| `--security-account-id` | Yes   | 12-digit AWS account ID of the security/management account.      | `<SECURITY_ACCOUNT_ID>`   |
| `--vpc-cidr`          | Yes      | VPC CIDR block for the new account's network.                    | `10.2.0.0/16`    |
| `--subnet-cidr`       | Yes      | Public subnet CIDR (must fall within the VPC CIDR).              | `10.2.1.0/24`    |
| `-h` / `--help`       | No       | Print usage information and exit.                                |                  |

### Alias naming rules

- Must start with a lowercase letter
- Only lowercase letters, digits, and hyphens allowed
- Follow the existing convention: `workload-c`, `workload-d`, etc.
- The alias is converted to underscores internally for Terraform identifiers (e.g., `workload-c` becomes `workload_c`)

## Examples

### Onboard a third workload account

```bash
./onboard_workload_account.sh \
  --alias workload-c \
  --account-id 111222333444 \
  --security-account-id <SECURITY_ACCOUNT_ID> \
  --vpc-cidr 10.2.0.0/16 \
  --subnet-cidr 10.2.1.0/24
```

### Onboard a fourth workload account

```bash
./onboard_workload_account.sh \
  --alias workload-d \
  --account-id 555666777888 \
  --security-account-id <SECURITY_ACCOUNT_ID> \
  --vpc-cidr 10.3.0.0/16 \
  --subnet-cidr 10.3.1.0/24
```

### Show help

```bash
./onboard_workload_account.sh --help
```

## What the Script Does

### Creates a new workload root

The script copies `workloads/_template-aws/` to `workloads/aws-<alias>/` and generates:

| File | Contents |
|------|----------|
| `terraform.tfvars` | Account-specific variable values (alias, account ID, CIDRs, security account ID) |
| `backend.tf` | S3 backend configuration with a unique state key (`workloads/<alias>/terraform.tfstate`) |

The template already includes `main.tf`, `variables.tf`, `outputs.tf`, `providers.tf`, and `versions.tf` -- these are copied as-is.

### Modifies hub and module files

| # | File | Change |
|---|------|--------|
| 1 | `modules/databricks/jobs/variables.tf` | Adds `<alias>_security_logs_bucket_name` variable |
| 2 | `modules/databricks/jobs/main.tf` | Adds `<alias>_bucket` to `common_params` locals |
| 3 | `hub/main.tf` | Adds `try()` block to extract bucket name from workload manifests |
| 4 | `notebooks/bronze/aws/01_cloudtrail.py` | Adds widget + source path |
| 5 | `notebooks/bronze/aws/02_vpc_flow.py` | Adds widget + source path |
| 6 | `notebooks/bronze/aws/03_guardduty.py` | Adds widget + source path |
| 7 | `notebooks/bronze/aws/04_config.py` | Adds widget + source path |

## What the Script Does NOT Do

- **Does not run Terraform** -- You must run `init`, `plan`, and `apply` yourself.
- **Does not modify the cloud-integration module** -- It uses `for_each` over the workloads list, so new workloads are handled automatically via `assemble-workloads.sh`.
- **Does not modify the security account foundation** -- The hub role uses wildcard IAM patterns that automatically match new workload buckets.
- **Does not create the AWS account** -- The account must already exist in your Organization.
- **Does not validate CIDR overlap** -- You must ensure CIDRs don't conflict.
- **Does not validate subnet is within VPC CIDR** -- You must ensure the subnet falls within the VPC range.

## Post-Script Steps

After the script completes, follow this sequence:

```bash
# 1. Review all changes
git diff

# 2. Initialize and apply the new workload root
cd workloads/aws-<alias>
terraform init
terraform apply

# 3. Assemble workload outputs for the hub
cd ../..
./scripts/assemble-workloads.sh

# 4. Apply the hub (picks up new workload via workloads.auto.tfvars.json)
cd hub
terraform fmt -recursive ..
terraform apply

# 5. Wait ~30 minutes for security data to start flowing
```

Replace `<alias>` with your workload alias (e.g., `workload-c`).

### Why this sequence?

1. The workload root must be applied first to create the VPC, data sources, and S3 bucket.
2. `assemble-workloads.sh` collects `workload_manifest` outputs from all applied workload roots into `hub/workloads.auto.tfvars.json`.
3. The hub root consumes the workload manifests to create external locations (via `cloud_integration` module's `for_each`) and wire up the jobs module with the new bucket name.

## Safety Features

### Idempotency check
The script checks if the workload directory already exists before making any changes. If found, it exits with an error:
```
ERROR: Workload directory already exists: workloads/aws-workload-c
       This alias appears to be already onboarded.
```

### Input validation
- Alias must match `^[a-z][a-z0-9-]+$`
- Account IDs must be exactly 12 digits
- CIDRs must match basic CIDR format (`x.x.x.x/x`)
- All five arguments are required

### Missing notebook handling
If a notebook file is missing from `notebooks/bronze/aws/`, the script prints a warning and continues with the remaining files.

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `ERROR: Script must be run from the repository root` | Script can't find `workloads/_template-aws/` or `hub/` | Run from the repo root: `cd databricks-security-lakehouse && ./onboard_workload_account.sh ...` |
| `ERROR: Workload directory already exists` | Alias was previously onboarded | Choose a different alias, or manually remove the previous workload directory if it was incomplete |
| sed inserts appear in wrong location | Anchor patterns in `hub/main.tf` or `jobs/main.tf` were modified | Check that the `workload_b` references haven't been altered |
| `terraform validate` fails after script | Formatting issues from sed insertions | Run `terraform fmt -recursive .` first, then validate again |
| `assemble-workloads.sh` skips the new workload | Workload root not yet applied | Run `terraform init && terraform apply` in `workloads/aws-<alias>/` first |

## Architecture Context

Each new workload account creates approximately 27 Terraform resources in the workload root:
- **Baseline** (~10): VPC, subnet, internet gateway, route table, security groups, EC2 instances (Linux + Windows), SSH key pair
- **Data sources** (~17): CloudTrail + S3 bucket, VPC Flow Logs, GuardDuty detector + S3 export, AWS Config recorder + delivery channel + rules, KMS key, read-only IAM role

The hub root automatically picks up the new workload via `assemble-workloads.sh`:
- **Cloud integration** (1): External location for the workload's S3 bucket (via `for_each`)
- **Jobs** (4 updated): Bronze notebook parameters updated with new bucket

The security account foundation and cloud-integration module require **no manual changes** -- they use wildcard IAM patterns and `for_each` loops that auto-match new workloads.
