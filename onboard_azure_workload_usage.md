# Usage Guide: onboard_azure_workload.sh

Automates all file modifications needed to add a new Azure workload subscription to the security lakehouse. The script creates a new workload Terraform root from the template and adds the new workload to Azure bronze notebooks. It does **not** run `terraform apply`.

Unlike the AWS onboarding script, this does **not** modify the jobs module or hub/main.tf -- the Azure expansion uses a dynamic workloads map that auto-discovers new workloads via `assemble-workloads.sh`.

## Prerequisites

Before running the script, ensure:

1. **Azure subscription** -- You need an active Azure subscription with its UUID subscription ID.
2. **Foundation applied** -- `foundations/azure-security/` must be applied (creates the Entra ID service principal):

   ```bash
   cd foundations/azure-security && terraform output service_principal_id
   ```

3. **CIDR planning** -- Choose a VNet CIDR and subnet CIDR that do not overlap with existing workloads:

   | Subscription | VNet CIDR | Subnet CIDR |
   |---|---|---|
   | azure-workload-a | 10.10.0.0/16 | 10.10.1.0/24 |
   | azure-workload-b | 10.11.0.0/16 | 10.11.1.0/24 |
   | azure-workload-c | 10.12.0.0/16 | 10.12.1.0/24 |

4. **Security account ID** -- You need the 12-digit AWS account ID of the security/management account (used for the S3 state backend).
5. **macOS or Linux** -- Compatible with macOS bash 3.x and Linux bash 4+.

## Syntax

```bash
./onboard_azure_workload.sh \
  --alias <name> \
  --subscription-id <uuid> \
  --service-principal-id <uuid> \
  --security-account-id <12-digit> \
  --vnet-cidr <cidr> \
  --subnet-cidr <cidr> \
  [--location <region>]
```

### Arguments

| Argument | Required | Description | Example |
|---|---|---|---|
| `--alias` | Yes | Workload alias. Lowercase, alphanumeric, hyphens only. The script prepends `azure-` for the directory and workload_alias. | `workload-b` |
| `--subscription-id` | Yes | Azure subscription ID (UUID format). | `12345678-1234-1234-1234-123456789abc` |
| `--service-principal-id` | Yes | Object ID of the Entra ID service principal (from foundation output). | `87654321-4321-4321-4321-cba987654321` |
| `--security-account-id` | Yes | 12-digit AWS account ID (for the S3 state backend bucket name). | `<SECURITY_ACCOUNT_ID>` |
| `--vnet-cidr` | Yes | VNet CIDR block for the subscription's network. | `10.11.0.0/16` |
| `--subnet-cidr` | Yes | Subnet CIDR (must fall within the VNet CIDR). | `10.11.1.0/24` |
| `--location` | No | Azure region (default: `eastus`). | `westus2` |
| `-h` / `--help` | No | Print usage information and exit. | |

### Alias naming rules

- Must start with a lowercase letter
- Only lowercase letters, digits, and hyphens allowed
- Follow the existing convention: `workload-b`, `workload-c`, etc.
- The script prepends `azure-` to create the full alias: `workload-b` becomes `azure-workload-b`
- The full alias is used as the directory name and `workload_alias` in terraform.tfvars
- Internally converted to underscores for notebook identifiers: `azure-workload-b` becomes `azure_workload_b`

## Examples

### Onboard a second Azure workload subscription

```bash
./onboard_azure_workload.sh \
  --alias workload-b \
  --subscription-id 11112222-3333-4444-5555-666677778888 \
  --service-principal-id 87654321-4321-4321-4321-cba987654321 \
  --security-account-id <SECURITY_ACCOUNT_ID> \
  --vnet-cidr 10.11.0.0/16 \
  --subnet-cidr 10.11.1.0/24
```

### Onboard in a different Azure region

```bash
./onboard_azure_workload.sh \
  --alias workload-c \
  --subscription-id aaaabbbb-cccc-dddd-eeee-ffffffffffff \
  --service-principal-id 87654321-4321-4321-4321-cba987654321 \
  --security-account-id <SECURITY_ACCOUNT_ID> \
  --vnet-cidr 10.12.0.0/16 \
  --subnet-cidr 10.12.1.0/24 \
  --location westus2
```

### Show help

```bash
./onboard_azure_workload.sh --help
```

## What the Script Does

### Creates a new workload root

The script copies `workloads/_template-azure/` to `workloads/azure-<alias>/` and generates:

| File | Contents |
|---|---|
| `terraform.tfvars` | Subscription-specific variable values (subscription ID, alias, CIDRs, service principal ID) |
| `backend.tf` | S3 backend configuration with a unique state key (`workloads/azure-<alias>/terraform.tfstate`) |

The template already includes `main.tf`, `variables.tf`, `outputs.tf`, `providers.tf`, and `versions.tf` -- these are copied as-is.

### Modifies notebook files

| # | File | Change |
|---|---|---|
| 1 | `notebooks/bronze/azure/01_activity_log.py` | Adds widget + source path |
| 2 | `notebooks/bronze/azure/02_vnet_flow.py` | Adds widget + source path |

### What it does NOT modify

Unlike the AWS onboarding script, these files are **not** touched:

| File | Why not |
|---|---|
| `modules/databricks/jobs/variables.tf` | Jobs module uses dynamic `workloads` map -- no per-workload variables |
| `modules/databricks/jobs/main.tf` | `common_params` generates `{alias}_storage_url` dynamically via `for_each` |
| `hub/main.tf` | Workloads passed directly as a dynamic map -- no `try()` blocks needed |

## What the Script Does NOT Do

- **Does not run Terraform** -- You must run `init`, `plan`, and `apply` yourself.
- **Does not modify the cloud-integration module** -- It uses `for_each` over the workloads list, so new workloads get external locations automatically.
- **Does not modify the security foundation** -- The service principal is shared across all Azure workloads.
- **Does not create the Azure subscription** -- The subscription must already exist.
- **Does not set hub azure_credentials** -- This is a one-time setup (see `onboarding_new_azure_accounts.md` Section 3).
- **Does not validate CIDR overlap** -- You must ensure CIDRs don't conflict.
- **Does not validate subnet is within VNet CIDR** -- You must ensure the subnet falls within the VNet range.

## Post-Script Steps

After the script completes, follow this sequence:

```bash
# 1. Review all changes
git diff

# 2. Initialize and apply the new workload root
cd workloads/azure-<alias>
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

Replace `<alias>` with your full workload alias (e.g., `azure-workload-b`).

### Why this sequence?

1. The workload root must be applied first to create the VNet, data sources, and ADLS Gen2 storage.
2. `assemble-workloads.sh` collects `workload_manifest` outputs from all applied workload roots into `hub/workloads.auto.tfvars.json`.
3. The hub root consumes the workload manifests to create external locations (via `cloud_integration` module's `for_each`) and update job parameters with the new storage URL.

## Safety Features

### Idempotency check
The script checks if the workload directory already exists before making any changes. If found, it exits with an error:
```
ERROR: Workload directory already exists: workloads/azure-workload-b
       This alias appears to be already onboarded.
```

### Input validation
- Alias must match `^[a-z][a-z0-9-]+$`
- Subscription ID and service principal ID must be valid UUIDs
- Security account ID must be exactly 12 digits
- CIDRs must match basic CIDR format (`x.x.x.x/x`)
- All six required arguments must be provided

### Missing notebook handling
If a notebook file is missing from `notebooks/bronze/azure/`, the script prints a warning and continues with the remaining files.

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `ERROR: Script must be run from the repository root` | Script can't find `workloads/_template-azure/` or `hub/` | Run from the repo root: `cd databricks-security-lakehouse && ./onboard_azure_workload.sh ...` |
| `ERROR: Workload directory already exists` | Alias was previously onboarded | Choose a different alias, or manually remove the previous workload directory if it was incomplete |
| sed inserts appear in wrong location | Anchor patterns in notebooks were modified | Check that the `azure_workload_a` references haven't been altered |
| `terraform validate` fails after script | Formatting issues from sed insertions | Run `terraform fmt -recursive .` first, then validate again |
| `assemble-workloads.sh` skips the new workload | Workload root not yet applied | Run `terraform init && terraform apply` in `workloads/azure-<alias>/` first |

## Architecture Context

Each new Azure workload subscription creates approximately 20 Terraform resources in the workload root:
- **Baseline** (~10): Resource group, VNet, subnet, NSG, NSG rules, public IPs, NICs, Linux VM, Windows VM, TLS private key
- **Data sources** (~10): ADLS Gen2 storage account, blob container, diagnostic setting (Activity Log), VNet Flow Log, service principal role assignment, Defender toggle, Resource Graph toggle

The hub root automatically picks up the new workload via `assemble-workloads.sh`:
- **Cloud integration** (1): External location for the workload's ADLS Gen2 storage (via `for_each`)
- **Jobs** (2 updated): Azure bronze notebook parameters updated with new storage URL

The security foundation, cloud-integration module, jobs module, and hub main.tf require **no code changes** -- they use shared credentials and dynamic `for_each` patterns that auto-match new workloads.
