# Onboarding New Azure Workload Subscriptions

This guide walks through adding a new Azure workload subscription to the security lakehouse. The process is repeatable -- each workload subscription uses its own independent Terraform root created from a template, and the hub automatically discovers new workloads via `assemble-workloads.sh`.

**Time estimate:** 20-30 minutes of Terraform work + ~30 minutes waiting for initial data to flow.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Architecture Context](#2-architecture-context)
3. [First-Time Azure Setup (one-time)](#3-first-time-azure-setup-one-time)
4. [Automated Onboarding (Recommended)](#4-automated-onboarding-recommended)
5. [Manual Step-by-Step: Add Azure Workload B](#5-manual-step-by-step-add-azure-workload-b)
6. [Apply Sequence](#6-apply-sequence)
7. [Validation Checklist](#7-validation-checklist)
8. [Why No Foundation, Jobs, or Hub Code Changes Are Needed](#8-why-no-foundation-jobs-or-hub-code-changes-are-needed)
9. [Scaling Notes](#9-scaling-notes)
10. [Troubleshooting](#10-troubleshooting)
11. [Quick Reference: Files Changed per New Subscription](#11-quick-reference-files-changed-per-new-subscription)

---

## 1. Prerequisites

Before starting, confirm:

| Requirement | Detail |
|---|---|
| **Azure subscription** | You need an active Azure subscription. The subscription ID is a UUID (e.g., `12345678-1234-1234-1234-123456789abc`). |
| **Azure CLI authenticated** | Run `az login` and verify with `az account show`. |
| **Foundation applied** | `foundations/azure-security/` must be applied first (creates the Entra ID service principal shared by all Azure workloads). |
| **Service principal ID** | From the foundation output: `cd foundations/azure-security && terraform output service_principal_id`. |
| **Hub azure_credentials set** | The hub root must have `azure_credentials` configured in its tfvars (see [Section 3](#3-first-time-azure-setup-one-time)). |
| **Security account ID** | The 12-digit AWS account ID for the security/management account (for the S3 state backend). |
| **Unique VNet CIDR** | Choose a `/16` CIDR that doesn't overlap with existing workloads (see CIDR table below). |

### CIDR Allocation Table

Azure workloads use the `10.10.x.0` range to visually separate from AWS workloads (`10.0.x.0`).

| Subscription | VNet CIDR | Subnet CIDR |
|---|---|---|
| Azure Workload A | `10.10.0.0/16` | `10.10.1.0/24` |
| Azure Workload B (new) | `10.11.0.0/16` | `10.11.1.0/24` |
| Azure Workload C (future) | `10.12.0.0/16` | `10.12.1.0/24` |
| Azure Workload D (future) | `10.13.0.0/16` | `10.13.1.0/24` |

---

## 2. Architecture Context

Each Azure workload subscription follows this pattern:

```
+------------------------------------------------------------+
|  Azure Workload Subscription (new)                         |
|                                                            |
|  Resource Group: lakehouse-rg-azure-workload-b             |
|  VNet + Subnet + NSG                                       |
|  +-- Linux VM (Ubuntu 22.04, Standard_B1s)                 |
|  +-- Windows VM (Windows Server 2022, Standard_B1s)        |
|                                                            |
|  Security Data Sources:                                    |
|  +-- Activity Log -> ADLS Gen2 (diagnostic setting)        |
|  +-- VNet Flow Logs -> ADLS Gen2 (JSON)                    |
|  +-- Defender for Cloud (toggle)                           |
|  +-- Resource Graph (toggle)                               |
|                                                            |
|  ADLS Gen2: lakehouseazureworkloadbXXXX                    |
|  Service principal granted Storage Blob Data Reader        |
+------------------------------------------------------------+
         |
         |  Databricks reads ADLS via Entra ID service principal
         |
         v
+------------------------------------------------------------+
|  Databricks (Unity Catalog)                                |
|  Azure storage credential -> External location -> bronze   |
+------------------------------------------------------------+
```

**Multi-root architecture:** Each workload has its own independent Terraform root under `workloads/azure-<alias>/`. The hub root discovers workloads via `assemble-workloads.sh`, which collects `workload_manifest` outputs into `hub/workloads.auto.tfvars.json`.

**Access chain:** Databricks -> Azure storage credential (Entra ID service principal) -> ADLS Gen2 storage account (Storage Blob Data Reader role assignment in the workload subscription).

**Key difference from AWS:** Azure workloads use a single shared service principal (from `foundations/azure-security/`) rather than per-account IAM roles. The service principal is granted reader access on each workload's ADLS Gen2 storage account automatically by the data-sources module.

---

## 3. First-Time Azure Setup (one-time)

If this is your first Azure workload, you need to complete these one-time steps before onboarding any subscriptions.

### 3.1 -- Apply the Azure security foundation

```bash
cd foundations/azure-security
terraform init
terraform apply
```

This creates:
- An Entra ID application and service principal
- An ADLS Gen2 managed storage account (for Databricks-managed data)
- A resource group in the security subscription

### 3.2 -- Collect foundation outputs

```bash
cd foundations/azure-security
terraform output service_principal_id    # Needed for each workload's terraform.tfvars
terraform output directory_id            # Needed for hub azure_credentials
terraform output application_id          # Needed for hub azure_credentials
terraform output -raw client_secret      # Needed for hub azure_credentials (sensitive)
```

### 3.3 -- Configure hub azure_credentials

Add to `hub/terraform.tfvars`:

```hcl
azure_credentials = {
  directory_id   = "<directory_id from output>"
  application_id = "<application_id from output>"
  client_secret  = "<client_secret from output>"
}
```

**This is a one-time setup.** Subsequent Azure workloads use the same service principal and credentials. Do not repeat this step.

---

## 4. Automated Onboarding (Recommended)

Use the `onboard_azure_workload.sh` script to automate all file modifications:

```bash
./onboard_azure_workload.sh \
  --alias workload-b \
  --subscription-id 12345678-1234-1234-1234-123456789abc \
  --service-principal-id 87654321-4321-4321-4321-cba987654321 \
  --security-account-id <SECURITY_ACCOUNT_ID> \
  --vnet-cidr 10.11.0.0/16 \
  --subnet-cidr 10.11.1.0/24
```

This creates the workload root from the template, generates `terraform.tfvars` and `backend.tf`, and updates the Azure bronze notebooks. See `onboard_azure_workload_usage.md` for full details.

After running the script, skip to [Section 6: Apply Sequence](#6-apply-sequence).

---

## 5. Manual Step-by-Step: Add Azure Workload B

All file paths are relative to the repository root.

### Step 5.1 -- Copy the workload template

```bash
cp -r workloads/_template-azure workloads/azure-workload-b
```

### Step 5.2 -- Create terraform.tfvars

**File:** `workloads/azure-workload-b/terraform.tfvars`

```hcl
subscription_id      = "XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX"  # <- Azure subscription ID
location             = "eastus"
workload_alias       = "azure-workload-b"
vnet_cidr            = "10.11.0.0/16"
subnet_cidr          = "10.11.1.0/24"
name_prefix          = "lakehouse"
service_principal_id = "XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX"  # <- from foundation output
```

**Important:** `workload_alias` must match the directory basename (`azure-workload-b`). This value flows into the workload manifest, Databricks external location names, and notebook job parameters.

### Step 5.3 -- Create backend.tf

**File:** `workloads/azure-workload-b/backend.tf`

```hcl
terraform {
  backend "s3" {
    bucket         = "security-lakehouse-tfstate-XXXXXXXXXXXX"   # <- AWS security account ID
    key            = "workloads/azure-workload-b/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "security-lakehouse-tflock"
    encrypt        = true
  }
}
```

**Note:** Azure workload roots store Terraform state in the shared AWS S3 backend. This is intentional -- all roots in this project share the same state backend in the security account.

### Step 5.4 -- Update Azure bronze notebooks

**Files:** Both notebooks in `notebooks/bronze/azure/`

Each notebook has a `source_paths` dictionary and widget parameters for workload storage URLs. Add the new workload to each.

**`notebooks/bronze/azure/01_activity_log.py`:**

```python
# Add widget for workload B
dbutils.widgets.text("azure_workload_b_storage_url", "", "Azure Workload B Storage URL")
azure_workload_b_storage_url = dbutils.widgets.get("azure_workload_b_storage_url")

# Update source_paths to include workload B
source_paths = {
    "azure_workload_a": f"{azure_workload_a_storage_url}insights-activity-logs/",
    "azure_workload_b": f"{azure_workload_b_storage_url}insights-activity-logs/",  # NEW
}
```

**`notebooks/bronze/azure/02_vnet_flow.py`:**

```python
# Add widget for workload B
dbutils.widgets.text("azure_workload_b_storage_url", "", "Azure Workload B Storage URL")
azure_workload_b_storage_url = dbutils.widgets.get("azure_workload_b_storage_url")

# Update source_paths to include workload B
source_paths = {
    "azure_workload_a": f"{azure_workload_a_storage_url}vnet-flow-logs/",
    "azure_workload_b": f"{azure_workload_b_storage_url}vnet-flow-logs/",  # NEW
}
```

---

## 6. Apply Sequence

The multi-root apply follows this order:

```
Step 1: Initialize and apply the new workload root
        cd workloads/azure-workload-b
        terraform init
        terraform plan    -> Verify ~20 new resources
        terraform apply
        -> Creates resource group, VNet, VMs, ADLS Gen2 storage,
           Activity Log diagnostic setting, VNet Flow Logs,
           service principal role assignment

Step 2: Assemble workload outputs
        cd ../..
        ./scripts/assemble-workloads.sh
        -> Collects workload_manifest outputs into hub/workloads.auto.tfvars.json

Step 3: Apply the hub
        cd hub
        terraform plan    -> Verify 1 new external location + updated job params
        terraform apply
        -> Creates Databricks external location (via cloud_integration for_each)
        -> Updates job parameters with new storage URL

Step 4: Wait 30 minutes for security data to flow
        -> Activity Log diagnostic setting starts immediately
        -> VNet Flow Logs may take 10-15 minutes
```

**Key difference from AWS onboarding:** No jobs module or hub/main.tf code changes are needed. The jobs module uses a dynamic `workloads` map that auto-generates parameters for any workload in the manifest. The cloud-integration module creates external locations via `for_each` over the same manifest.

---

## 7. Validation Checklist

After apply, verify each layer:

### Azure Infrastructure
```bash
# Verify resource group exists
az group show --name lakehouse-rg-azure-workload-b

# Verify VNet exists
az network vnet show --resource-group lakehouse-rg-azure-workload-b \
  --name lakehouse-azure-workload-b-vnet

# Verify ADLS Gen2 storage account exists
az storage account show --resource-group lakehouse-rg-azure-workload-b \
  --name lakehouseazureworkloadbXXXX

# Verify diagnostic setting for Activity Log
az monitor diagnostic-settings subscription list \
  --subscription XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX

# Verify VNet Flow Logs
az network watcher flow-log list --resource-group lakehouse-rg-azure-workload-b
```

### Service Principal Access
```bash
# Verify the service principal has Storage Blob Data Reader on the storage account
az role assignment list \
  --assignee SERVICE_PRINCIPAL_ID \
  --scope /subscriptions/SUBSCRIPTION_ID/resourceGroups/lakehouse-rg-azure-workload-b
```

### Databricks
```sql
-- Verify external location is accessible
LIST 'abfss://security-logs@lakehouseazureworkloadbXXXX.dfs.core.windows.net/';

-- After data flows, verify bronze tables include new subscription data
SELECT DISTINCT _source_file
FROM security_poc.bronze.activity_log_raw
WHERE _source_file LIKE '%azure-workload-b%';
```

---

## 8. Why No Foundation, Jobs, or Hub Code Changes Are Needed

The Azure expansion was designed with dynamic discovery patterns that eliminate per-workload code changes:

| Component | Why It Auto-Matches |
|---|---|
| **Service principal** | Created once in `foundations/azure-security/`. Each workload's data-sources module grants it `Storage Blob Data Reader` on the workload's ADLS Gen2 account. No changes to the foundation needed. |
| **Cloud integration** | Uses `for_each` over `var.workloads`. New workloads get external locations automatically. Azure workloads are routed to the Azure storage credential via `cloud == "azure"` conditional. |
| **Jobs module** | Uses a dynamic `workloads` map: `{ for alias, w in var.workloads : "${alias}_storage_url" => w.storage_url }`. New workloads get job parameters automatically. |
| **Hub main.tf** | Passes `var.workloads` directly to cloud-integration and transforms it into the dynamic map for jobs. No per-workload `try()` blocks needed (unlike the older AWS pattern). |
| **Hub azure_credentials** | Set once when the first Azure workload is added. All Azure workloads share the same Entra ID service principal. |

**The only non-dynamic pieces are the notebooks.** Notebook widget definitions and `source_paths` dictionaries are hardcoded per-workload. This is the only file modification needed when adding a new Azure subscription.

---

## 9. Scaling Notes

### Current Limitations

**Notebook storage URL parameters are positional.** The current notebooks use named widget parameters (`azure_workload_a_storage_url`, `azure_workload_b_storage_url`, etc.). At ~5+ subscriptions, consider refactoring notebooks to accept a JSON-encoded list of storage URLs instead.

**External locations are per-workload.** Each workload gets one external location via `for_each`. At scale, this is manageable since external locations are lightweight Databricks metadata objects.

### Naming Convention

All resources follow the pattern `lakehouse-{alias}-{resource_type}`:

| Resource | Naming Pattern | Example |
|---|---|---|
| Workload root | `workloads/azure-{alias}/` | `workloads/azure-workload-b/` |
| Resource group | `lakehouse-rg-azure-{alias}` | `lakehouse-rg-azure-workload-b` |
| VNet | `lakehouse-azure-{alias}-vnet` | `lakehouse-azure-workload-b-vnet` |
| ADLS Gen2 | `lakehouseazure{alias}XXXX` (no hyphens) | `lakehouseazureworkloadbXXXX` |
| External Location | `security-logs-azure-{alias}` | `security-logs-azure-workload-b` |

The `workload_alias` must be unique and should follow the `azure-workload-{letter}` convention.

### What If You Need to Remove a Workload Subscription?

1. Remove the workload root directory (`workloads/azure-workload-b/`)
2. Remove the widget and source path from each notebook in `notebooks/bronze/azure/`
3. Re-run `./scripts/assemble-workloads.sh` (removes the workload from the manifest)
4. Run `cd hub && terraform apply` -- Terraform will destroy the external location
5. Run `cd workloads/azure-workload-b && terraform destroy` to remove Azure resources
6. **Caution:** This destroys the ADLS Gen2 storage account and all security logs in it. Consider backing up first.

---

## 10. Troubleshooting

### "AuthorizationFailed" when applying the workload root

The Azure CLI session needs access to the target subscription. Verify:

```bash
az account set --subscription XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
az account show
```

### Service principal role assignment fails

If the service principal hasn't been created yet, run `foundations/azure-security/` first:

```bash
cd foundations/azure-security && terraform apply
```

Then pass the `service_principal_id` output to the workload's `terraform.tfvars`.

### Databricks external location validation fails

If the external location creation fails with a permissions error:
1. Verify the service principal has `Storage Blob Data Reader` on the workload's ADLS Gen2 account
2. Verify `azure_credentials` in hub terraform.tfvars matches the foundation outputs
3. Verify the Azure storage credential was created in Databricks: check `hub/` state for `databricks_storage_credential.azure`

### Activity Log diagnostic setting conflict

Azure allows only one diagnostic setting per category per subscription. If a diagnostic setting already exists for the Activity Log:

```bash
az monitor diagnostic-settings subscription list \
  --subscription XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
```

Remove the conflicting setting or import it into Terraform.

### VNet Flow Logs not appearing

VNet Flow Logs require Network Watcher to be enabled in the region. Azure typically enables it automatically, but verify:

```bash
az network watcher list -o table
```

If not enabled:

```bash
az network watcher configure --resource-group NetworkWatcherRG \
  --locations eastus --enabled
```

### `assemble-workloads.sh` skips the new workload

The workload root must be initialized and applied before `assemble-workloads.sh` can collect its outputs. Run `terraform init && terraform apply` in `workloads/azure-<alias>/` first.

### "empty directory" in Auto Loader notebook

This is normal -- it means no files exist yet at the ADLS Gen2 path. Wait 30 minutes for data to flow. The notebooks handle this gracefully with a try/except.

---

## 11. Quick Reference: Files Changed per New Subscription

### New files (created from template)

| File | Contents |
|---|---|
| `workloads/azure-<alias>/` | Full workload root (copied from `_template-azure/`) |
| `workloads/azure-<alias>/terraform.tfvars` | Subscription-specific values |
| `workloads/azure-<alias>/backend.tf` | S3 backend with unique state key |

### Modified files

| File | Change Type | What to Add |
|---|---|---|
| `notebooks/bronze/azure/01_activity_log.py` | Update paths | Add widget + `source_paths` entry |
| `notebooks/bronze/azure/02_vnet_flow.py` | Update paths | Add widget + `source_paths` entry |

### Auto-generated files (no manual changes)

| File | How it's populated |
|---|---|
| `hub/workloads.auto.tfvars.json` | Generated by `assemble-workloads.sh` from workload outputs |

**Total: 1 new workload root + 2 modified notebooks, ~20 new Azure resources, 1 new Databricks external location, 2 updated Databricks jobs.**

**No changes needed:** Azure security foundation, cloud-integration module (uses `for_each`), jobs module (dynamic workloads map), hub/main.tf, unity-catalog module, workspace-config module, hub azure_credentials (set once).

### Comparison with AWS Onboarding

| Aspect | AWS | Azure |
|---|---|---|
| Files modified per account | 7 | 2 |
| Jobs module changes | Yes (per-workload variables) | No (dynamic map) |
| Hub main.tf changes | Yes (per-workload `try()` blocks) | No (dynamic `for_each`) |
| Foundation changes | None | None |
| Notebook changes | 4 notebooks | 2 notebooks |
| Cloud resources created | ~27 | ~20 |
| State backend | S3 (AWS) | S3 (AWS) — shared backend |
