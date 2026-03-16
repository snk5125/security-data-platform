# Usage Guide: onboard_workload_account.sh

Automates all Terraform and notebook file modifications needed to add a new AWS workload account to the security lakehouse. The script modifies 16 files across the repository but does **not** run `terraform apply`.

## Prerequisites

Before running the script, ensure:

1. **AWS Organizations membership** — The new account must be a member of your AWS Organization with the `OrganizationAccountAccessRole` IAM role available (created automatically by AWS Organizations).
2. **CIDR planning** — Choose a VPC CIDR and subnet CIDR that do not overlap with existing workloads:

   | Account    | VPC CIDR      | Subnet CIDR   |
   |------------|---------------|----------------|
   | workload-a | 10.0.0.0/16   | 10.0.1.0/24   |
   | workload-b | 10.1.0.0/16   | 10.1.1.0/24   |
   | workload-c | 10.2.0.0/16   | 10.2.1.0/24   |
   | workload-d | 10.3.0.0/16   | 10.3.1.0/24   |

3. **macOS or Linux** — Compatible with macOS bash 3.x and Linux bash 4+.

## Syntax

```bash
./onboard_workload_account.sh \
  --alias <name> \
  --account-id <12-digit> \
  --vpc-cidr <cidr> \
  --subnet-cidr <cidr>
```

### Arguments

| Argument        | Required | Description                                                      | Example          |
|-----------------|----------|------------------------------------------------------------------|------------------|
| `--alias`       | Yes      | Workload account alias. Lowercase, alphanumeric, hyphens only.   | `workload-c`     |
| `--account-id`  | Yes      | 12-digit AWS account ID of the new workload account.             | `123456789012`   |
| `--vpc-cidr`    | Yes      | VPC CIDR block for the new account's network.                    | `10.2.0.0/16`    |
| `--subnet-cidr` | Yes      | Public subnet CIDR (must fall within the VPC CIDR).              | `10.2.1.0/24`    |
| `-h` / `--help` | No       | Print usage information and exit.                                |                  |

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
  --vpc-cidr 10.2.0.0/16 \
  --subnet-cidr 10.2.1.0/24
```

### Onboard a fourth workload account

```bash
./onboard_workload_account.sh \
  --alias workload-d \
  --account-id 555666777888 \
  --vpc-cidr 10.3.0.0/16 \
  --subnet-cidr 10.3.1.0/24
```

### Show help

```bash
./onboard_workload_account.sh --help
```

## What the Script Modifies

The script touches 16 files in a single run:

| # | File | Change |
|---|------|--------|
| 1 | `environments/poc/variables.tf` | Adds `<alias>_account_id` variable |
| 2 | `environments/poc/terraform.tfvars` | Adds account ID value |
| 3 | `environments/poc/providers.tf` | Adds `aws.<alias>` provider alias |
| 4 | `environments/poc/main.tf` | Adds baseline module (Phase 3) |
| 5 | `environments/poc/main.tf` | Adds data sources module (Phase 4) |
| 6 | `environments/poc/main.tf` | Updates cloud_integration module inputs |
| 7 | `environments/poc/main.tf` | Updates bronze_ingestion module inputs |
| 8 | `environments/poc/outputs.tf` | Adds 6 output blocks |
| 9 | `modules/databricks/cloud-integration/variables.tf` | Adds bucket variable |
| 10 | `modules/databricks/cloud-integration/main.tf` | Adds external location resource |
| 11 | `modules/databricks/cloud-integration/outputs.tf` | Adds external location output |
| 12 | `modules/databricks/jobs/variables.tf` | Adds bucket variable |
| 13 | `modules/databricks/jobs/main.tf` | Updates `common_params` locals |
| 14 | `notebooks/bronze/01_bronze_cloudtrail.py` | Adds widget + source path |
| 15 | `notebooks/bronze/02_bronze_vpc_flow.py` | Adds widget + source path |
| 16 | `notebooks/bronze/03_bronze_guardduty.py` | Adds widget + source path |
| 17 | `notebooks/bronze/04_bronze_config.py` | Adds widget + source path |

## What the Script Does NOT Do

- **Does not run Terraform** — You must run `plan` and `apply` yourself.
- **Does not modify the security account baseline** — The hub role uses wildcard IAM patterns (`arn:aws:s3:::*-security-logs-*`) that automatically match new workload buckets.
- **Does not create the AWS account** — The account must already exist in your Organization.
- **Does not validate CIDR overlap** — You must ensure CIDRs don't conflict.
- **Does not validate subnet is within VPC CIDR** — You must ensure the subnet falls within the VPC range.

## Post-Script Steps

After the script completes, follow this sequence:

```bash
# 1. Review all changes
git diff  # or manually inspect modified files

# 2. Format Terraform
cd environments/poc
terraform fmt -recursive ../..

# 3. Validate configuration
terraform validate

# 4. Preview changes
terraform plan

# 5. Staged apply (recommended)
terraform apply -target=module.<alias>_baseline
terraform apply -target=module.<alias>_data_sources
terraform apply   # applies remaining changes (cloud integration, jobs, etc.)
```

Replace `<alias>` with your underscore-variant alias (e.g., `workload_c`).

### Why staged apply?

The data sources module depends on outputs from the baseline module (VPC ID, etc.). Applying in stages ensures Terraform has the necessary values at each step. A single `terraform apply` also works if Terraform can resolve the dependency graph, but staged apply is safer.

## Safety Features

### Idempotency check
The script checks if the alias already exists in `variables.tf` before making any changes. If found, it exits with an error:
```
ERROR: Account alias 'workload-c' appears to be already onboarded.
       Found 'workload_c_account_id' in environments/poc/variables.tf
```

### Input validation
- Alias must match `^[a-z][a-z0-9-]+$`
- Account ID must be exactly 12 digits
- CIDRs must match basic CIDR format (`x.x.x.x/x`)
- All four arguments are required

### Missing notebook handling
If a notebook file is missing from `notebooks/bronze/`, the script prints a warning and continues with the remaining files.

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `declare: -A: invalid option` | Running with bash 4+ syntax on macOS default bash 3 | This was fixed — the script uses indexed arrays. Ensure you're running the latest version. |
| `ERROR: Script must be run from the repository root` | Script can't find `environments/poc/` | Run from the repo root: `cd databricks-security-lakehouse && ./onboard_workload_account.sh ...` |
| `ERROR: Account alias already onboarded` | Alias was previously onboarded | Choose a different alias, or manually remove the previous onboarding if it was incomplete |
| sed inserts appear in wrong location | Anchor comments were modified | Check that phase header comments in `main.tf` and `outputs.tf` haven't been altered |
| `terraform validate` fails after script | Formatting issues from sed insertions | Run `terraform fmt -recursive ../..` first, then validate again |

## Architecture Context

Each new workload account creates approximately 27 Terraform resources:
- **Baseline** (~10): VPC, subnet, internet gateway, route table, security groups, EC2 instances (Linux + Windows), SSH key pair
- **Data sources** (~17): CloudTrail + S3 bucket, VPC Flow Logs, GuardDuty detector + S3 export, AWS Config recorder + delivery channel + rules, KMS key, read-only IAM role
- **Databricks** (1): External location for the workload's S3 bucket

The security account baseline requires **no changes** — its hub role IAM policy uses wildcard patterns that auto-match new workload buckets and KMS keys.
