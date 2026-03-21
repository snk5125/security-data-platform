# Usage Guide: onboard_gcp_workload.sh

Automates all file modifications needed to add a new GCP workload project to the security lakehouse. The script creates a new workload Terraform root from the template and adds the new workload to GCP bronze notebooks. It does **not** run `terraform apply`.

Like the Azure onboarding script, this does **not** modify the jobs module or hub/main.tf -- the GCP expansion uses a dynamic workloads map that auto-discovers new workloads via `assemble-workloads.sh`.

## Prerequisites

Before running the script, ensure:

1. **GCP project** -- You need an active GCP project with its project ID (e.g., `my-gcp-project-456`).
2. **gcloud authenticated** -- Run `gcloud auth application-default login` and verify with `gcloud config get-value project`.
3. **Foundation applied** -- `foundations/gcp-security/` must be applied (creates the GCP service account):

   ```bash
   cd foundations/gcp-security && terraform output service_account_email
   ```

4. **CIDR planning** -- Choose a VPC subnet CIDR that does not overlap with existing workloads:

   | Project | VPC CIDR |
   |---|---|
   | gcp-workload-a | 10.20.0.0/24 |
   | gcp-workload-b | 10.21.0.0/24 |
   | gcp-workload-c | 10.22.0.0/24 |

5. **Security account ID** -- You need the 12-digit AWS account ID of the security/management account (used for the S3 state backend).
6. **macOS or Linux** -- Compatible with macOS bash 3.x and Linux bash 4+.

## Syntax

```bash
./onboard_gcp_workload.sh \
  --alias <name> \
  --project-id <id> \
  --service-account-email <email> \
  --security-account-id <12-digit> \
  --vpc-cidr <cidr> \
  [--region <region>] \
  [--zone <zone>] \
  [--enable-scc]
```

### Arguments

| Argument | Required | Description | Example |
|---|---|---|---|
| `--alias` | Yes | Workload alias. Lowercase, alphanumeric, hyphens only. The script prepends `gcp-` for the directory and workload_alias. | `workload-b` |
| `--project-id` | Yes | GCP project ID. Lowercase alphanumeric with hyphens. | `my-gcp-project-456` |
| `--service-account-email` | Yes | Email of the Databricks service account (from foundation output). | `lakehouse-sa@my-proj.iam.gserviceaccount.com` |
| `--security-account-id` | Yes | 12-digit AWS account ID (for the S3 state backend bucket name). | `<SECURITY_ACCOUNT_ID>` |
| `--vpc-cidr` | Yes | VPC subnet CIDR block for the project's network. | `10.21.0.0/24` |
| `--region` | No | GCP region (default: `us-central1`). | `us-east1` |
| `--zone` | No | GCP zone (default: `us-central1-a`). Must be within the specified region. | `us-east1-b` |
| `--enable-scc` | No | Enable SCC Findings export (default: false). | |
| `-h` / `--help` | No | Print usage information and exit. | |

### Alias naming rules

- Must start with a lowercase letter
- Only lowercase letters, digits, and hyphens allowed
- Follow the existing convention: `workload-b`, `workload-c`, etc.
- The script prepends `gcp-` to create the full alias: `workload-b` becomes `gcp-workload-b`
- The full alias is used as the directory name and `workload_alias` in terraform.tfvars
- Internally converted to underscores for notebook identifiers: `gcp-workload-b` becomes `gcp_workload_b`

## Examples

### Onboard a second GCP workload project

```bash
./onboard_gcp_workload.sh \
  --alias workload-b \
  --project-id my-gcp-project-456 \
  --service-account-email lakehouse-sa@my-security-proj.iam.gserviceaccount.com \
  --security-account-id <SECURITY_ACCOUNT_ID> \
  --vpc-cidr 10.21.0.0/24
```

### Onboard in a different GCP region with SCC enabled

```bash
./onboard_gcp_workload.sh \
  --alias workload-c \
  --project-id my-gcp-project-789 \
  --service-account-email lakehouse-sa@my-security-proj.iam.gserviceaccount.com \
  --security-account-id <SECURITY_ACCOUNT_ID> \
  --vpc-cidr 10.22.0.0/24 \
  --region us-east1 \
  --zone us-east1-b \
  --enable-scc
```

### Show help

```bash
./onboard_gcp_workload.sh --help
```

## What the Script Does

### Creates a new workload root

The script copies `workloads/_template-gcp/` to `workloads/gcp-<alias>/` and generates:

| File | Contents |
|---|---|
| `terraform.tfvars` | Project-specific variable values (project ID, alias, CIDR, service account email, region, zone, SCC toggle) |
| `backend.tf` | S3 backend configuration with a unique state key (`workloads/gcp-<alias>/terraform.tfstate`) |

The template already includes `main.tf`, `variables.tf`, `outputs.tf`, `providers.tf`, and `versions.tf` -- these are copied as-is.

### Modifies notebook files

| # | File | Change |
|---|---|---|
| 1 | `notebooks/bronze/gcp/01_cloud_audit_logs.py` | Adds widget + source path |
| 2 | `notebooks/bronze/gcp/02_vpc_flow_logs.py` | Adds widget + source path |
| 3 | `notebooks/bronze/gcp/03_asset_inventory.py` | Adds widget + source path |
| 4 | `notebooks/bronze/gcp/04_scc_findings.py` | Adds widget + source path |

### What it does NOT modify

Like the Azure onboarding script, these files are **not** touched:

| File | Why not |
|---|---|
| `modules/databricks/jobs/variables.tf` | Jobs module uses dynamic `workloads` map -- no per-workload variables |
| `modules/databricks/jobs/main.tf` | `common_params` generates `{alias}_storage_url` dynamically via `for_each` |
| `hub/main.tf` | Workloads passed directly as a dynamic map -- no `try()` blocks needed |

## What the Script Does NOT Do

- **Does not run Terraform** -- You must run `init`, `plan`, and `apply` yourself.
- **Does not modify the cloud-integration module** -- It uses `for_each` over the workloads list, so new workloads get external locations automatically.
- **Does not modify the security foundation** -- The service account is shared across all GCP workloads.
- **Does not create the GCP project** -- The project must already exist.
- **Does not set hub gcp_credentials** -- This is a one-time setup (see `onboarding_new_gcp_accounts.md` Section 3).
- **Does not validate CIDR overlap** -- You must ensure CIDRs don't conflict.
- **Does not validate zone is within region** -- You must ensure the zone belongs to the specified region.
- **Does not enable GCP APIs** -- Required APIs (Compute, Logging, Asset Inventory, SCC) must be enabled in the project.

## Post-Script Steps

After the script completes, follow this sequence:

```bash
# 1. Review all changes
git diff

# 2. Initialize and apply the new workload root
cd workloads/gcp-<alias>
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

Replace `<alias>` with your full workload alias (e.g., `gcp-workload-b`).

### Why this sequence?

1. The workload root must be applied first to create the VPC, data sources, and GCS storage.
2. `assemble-workloads.sh` collects `workload_manifest` outputs from all applied workload roots into `hub/workloads.auto.tfvars.json`.
3. The hub root consumes the workload manifests to create external locations (via `cloud_integration` module's `for_each`) and update job parameters with the new storage URL.

## Safety Features

### Idempotency check
The script checks if the workload directory already exists before making any changes. If found, it exits with an error:
```
ERROR: Workload directory already exists: workloads/gcp-workload-b
       This alias appears to be already onboarded.
```

### Input validation
- Alias must match `^[a-z][a-z0-9-]+$`
- Project ID must match `^[a-z][a-z0-9-]+$`
- Service account email must be a valid email format
- Security account ID must be exactly 12 digits
- CIDR must match basic CIDR format (`x.x.x.x/x`)
- All five required arguments must be provided

### Missing notebook handling
If a notebook file is missing from `notebooks/bronze/gcp/`, the script prints a warning and continues with the remaining files.

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `ERROR: Script must be run from the repository root` | Script can't find `workloads/_template-gcp/` or `hub/` | Run from the repo root: `cd databricks-security-lakehouse && ./onboard_gcp_workload.sh ...` |
| `ERROR: Workload directory already exists` | Alias was previously onboarded | Choose a different alias, or manually remove the previous workload directory if it was incomplete |
| sed inserts appear in wrong location | Anchor patterns in notebooks were modified | Check that the `gcp_workload_a` references haven't been altered |
| `terraform validate` fails after script | Formatting issues from sed insertions | Run `terraform fmt -recursive .` first, then validate again |
| `assemble-workloads.sh` skips the new workload | Workload root not yet applied | Run `terraform init && terraform apply` in `workloads/gcp-<alias>/` first |

## Architecture Context

Each new GCP workload project creates approximately 15-20 Terraform resources in the workload root:
- **Baseline** (~8): VPC network, subnet, firewall rules, public IPs, Linux VM, Windows VM, TLS private key
- **Data sources** (~7-10): GCS bucket, Cloud Logging sink (Audit Logs), Cloud Logging sink (VPC Flow Logs), service account IAM binding, Asset Inventory export, and optionally SCC Findings export

The hub root automatically picks up the new workload via `assemble-workloads.sh`:
- **Cloud integration** (1): External location for the workload's GCS bucket (via `for_each`)
- **Jobs** (4 updated): GCP bronze notebook parameters updated with new storage URL

The security foundation, cloud-integration module, jobs module, and hub main.tf require **no code changes** -- they use shared credentials and dynamic `for_each` patterns that auto-match new workloads.
