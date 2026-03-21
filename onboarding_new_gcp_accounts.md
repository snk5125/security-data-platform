# Onboarding New GCP Workload Projects

This guide walks through adding a new GCP workload project to the security lakehouse. The process is repeatable -- each workload project uses its own independent Terraform root created from a template, and the hub automatically discovers new workloads via `assemble-workloads.sh`.

**Time estimate:** 20-30 minutes of Terraform work + ~30 minutes waiting for initial data to flow.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Architecture Context](#2-architecture-context)
3. [First-Time GCP Setup (one-time)](#3-first-time-gcp-setup-one-time)
4. [Automated Onboarding (Recommended)](#4-automated-onboarding-recommended)
5. [Manual Step-by-Step: Add GCP Workload B](#5-manual-step-by-step-add-gcp-workload-b)
6. [Apply Sequence](#6-apply-sequence)
7. [Validation Checklist](#7-validation-checklist)
8. [Why No Foundation, Jobs, or Hub Code Changes Are Needed](#8-why-no-foundation-jobs-or-hub-code-changes-are-needed)
9. [Scaling Notes](#9-scaling-notes)
10. [Troubleshooting](#10-troubleshooting)
11. [Quick Reference: Files Changed per New Project](#11-quick-reference-files-changed-per-new-project)

---

## 1. Prerequisites

Before starting, confirm:

| Requirement | Detail |
|---|---|
| **GCP project** | You need an active GCP project. The project ID is a lowercase string with hyphens (e.g., `my-gcp-project-456`). |
| **gcloud authenticated** | Run `gcloud auth application-default login` and verify with `gcloud config get-value project`. |
| **Required APIs enabled** | Compute Engine, Cloud Logging, Cloud Asset Inventory, and optionally Security Command Center must be enabled in the project. |
| **Foundation applied** | `foundations/gcp-security/` must be applied first (creates the GCP service account shared by all GCP workloads). |
| **Service account email** | From the foundation output: `cd foundations/gcp-security && terraform output service_account_email`. |
| **Hub gcp_credentials set** | The hub root must have `gcp_credentials` configured in its tfvars (see [Section 3](#3-first-time-gcp-setup-one-time)). |
| **Security account ID** | The 12-digit AWS account ID for the security/management account (for the S3 state backend). |
| **Unique VPC CIDR** | Choose a `/24` CIDR that doesn't overlap with existing workloads (see CIDR table below). |

### CIDR Allocation Table

GCP workloads use the `10.20.x.0` range to visually separate from AWS workloads (`10.0.x.0`) and Azure workloads (`10.10.x.0`).

| Project | VPC CIDR |
|---|---|
| GCP Workload A | `10.20.0.0/24` |
| GCP Workload B (new) | `10.21.0.0/24` |
| GCP Workload C (future) | `10.22.0.0/24` |
| GCP Workload D (future) | `10.23.0.0/24` |

---

## 2. Architecture Context

Each GCP workload project follows this pattern:

```
+------------------------------------------------------------+
|  GCP Workload Project (new)                                |
|                                                            |
|  VPC Network: lakehouse-gcp-workload-b-vpc                 |
|  Subnet + Firewall Rules                                   |
|  +-- Linux VM (e2-micro, Debian)                           |
|  +-- Windows VM (e2-medium, Windows Server 2022)           |
|                                                            |
|  Security Data Sources:                                    |
|  +-- Cloud Audit Logs -> GCS (via Cloud Logging sink)      |
|  +-- VPC Flow Logs -> GCS (via Cloud Logging sink)         |
|  +-- Cloud Asset Inventory -> GCS (export)                 |
|  +-- SCC Findings -> GCS (optional, if enabled)            |
|                                                            |
|  GCS Bucket: lakehouse-gcp-workload-b-security-logs-XXXX   |
|  Service account granted Storage Object Viewer             |
+------------------------------------------------------------+
         |
         |  Databricks reads GCS via service account key
         |
         v
+------------------------------------------------------------+
|  Databricks (Unity Catalog)                                |
|  GCP storage credential -> External location -> bronze     |
+------------------------------------------------------------+
```

**Multi-root architecture:** Each workload has its own independent Terraform root under `workloads/gcp-<alias>/`. The hub root discovers workloads via `assemble-workloads.sh`, which collects `workload_manifest` outputs into `hub/workloads.auto.tfvars.json`.

**Access chain:** Databricks -> GCP storage credential (service account key) -> GCS bucket (Storage Object Viewer IAM binding in the workload project).

**Key difference from AWS and Azure:** GCP workloads use a single shared service account (from `foundations/gcp-security/`) rather than per-account IAM roles or Entra ID service principals. The service account is granted reader access on each workload's GCS bucket automatically by the data-sources module.

---

## 3. First-Time GCP Setup (one-time)

If this is your first GCP workload, you need to complete these one-time steps before onboarding any projects.

### 3.1 -- Apply the GCP security foundation

```bash
cd foundations/gcp-security
terraform init
terraform apply
```

This creates:
- A GCP service account for Databricks access
- A service account key (base64-encoded JSON)
- Foundation-level resources in the security project

### 3.2 -- Collect foundation outputs

```bash
cd foundations/gcp-security
terraform output service_account_email      # Needed for each workload's terraform.tfvars
terraform output -raw service_account_private_key  # Needed for hub gcp_credentials (sensitive)
terraform output project_id                 # Security project ID (for reference)
```

### 3.3 -- Configure hub gcp_credentials

Add to `hub/terraform.tfvars`:

```hcl
gcp_credentials = {
  service_account_email = "<service_account_email from output>"
  private_key           = "<service_account_private_key from output>"
}
```

**This is a one-time setup.** Subsequent GCP workloads use the same service account and credentials. Do not repeat this step.

---

## 4. Automated Onboarding (Recommended)

Use the `onboard_gcp_workload.sh` script to automate all file modifications:

```bash
./onboard_gcp_workload.sh \
  --alias workload-b \
  --project-id my-gcp-project-456 \
  --service-account-email lakehouse-sa@my-security-proj.iam.gserviceaccount.com \
  --security-account-id <SECURITY_ACCOUNT_ID> \
  --vpc-cidr 10.21.0.0/24
```

This creates the workload root from the template, generates `terraform.tfvars` and `backend.tf`, and updates the GCP bronze notebooks. See `onboard_gcp_workload_usage.md` for full details.

After running the script, skip to [Section 6: Apply Sequence](#6-apply-sequence).

---

## 5. Manual Step-by-Step: Add GCP Workload B

All file paths are relative to the repository root.

### Step 5.1 -- Copy the workload template

```bash
cp -r workloads/_template-gcp workloads/gcp-workload-b
```

### Step 5.2 -- Create terraform.tfvars

**File:** `workloads/gcp-workload-b/terraform.tfvars`

```hcl
project_id            = "my-gcp-project-456"          # <- GCP project ID
region                = "us-central1"
zone                  = "us-central1-a"
workload_alias        = "gcp-workload-b"
vpc_cidr              = "10.21.0.0/24"
name_prefix           = "lakehouse"
service_account_email = "lakehouse-sa@my-proj.iam.gserviceaccount.com"  # <- from foundation output
enable_scc            = false
```

**Important:** `workload_alias` must match the directory basename (`gcp-workload-b`). This value flows into the workload manifest, Databricks external location names, and notebook job parameters.

### Step 5.3 -- Create backend.tf

**File:** `workloads/gcp-workload-b/backend.tf`

```hcl
terraform {
  backend "s3" {
    bucket         = "security-lakehouse-tfstate-XXXXXXXXXXXX"   # <- AWS security account ID
    key            = "workloads/gcp-workload-b/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "security-lakehouse-tflock"
    encrypt        = true
  }
}
```

**Note:** GCP workload roots store Terraform state in the shared AWS S3 backend. This is intentional -- all roots in this project share the same state backend in the security account.

### Step 5.4 -- Update GCP bronze notebooks

**Files:** All four notebooks in `notebooks/bronze/gcp/`

Each notebook has a `source_paths` dictionary and widget parameters for workload storage URLs. Add the new workload to each.

**`notebooks/bronze/gcp/01_cloud_audit_logs.py`:**

```python
# Add widget for workload B
dbutils.widgets.text("gcp_workload_b_storage_url", "", "GCP Workload B Storage URL")
gcp_workload_b_storage_url = dbutils.widgets.get("gcp_workload_b_storage_url")

# Update source_paths to include workload B
source_paths = {
    "gcp_workload_a": f"{gcp_workload_a_storage_url}cloudaudit.googleapis.com/",
    "gcp_workload_b": f"{gcp_workload_b_storage_url}cloudaudit.googleapis.com/",  # NEW
}
```

**`notebooks/bronze/gcp/02_vpc_flow_logs.py`:**

```python
# Add widget for workload B
dbutils.widgets.text("gcp_workload_b_storage_url", "", "GCP Workload B Storage URL")
gcp_workload_b_storage_url = dbutils.widgets.get("gcp_workload_b_storage_url")

# Update source_paths to include workload B
source_paths = {
    "gcp_workload_a": f"{gcp_workload_a_storage_url}compute.googleapis.com%2Fvpc_flows/",
    "gcp_workload_b": f"{gcp_workload_b_storage_url}compute.googleapis.com%2Fvpc_flows/",  # NEW
}
```

**`notebooks/bronze/gcp/03_asset_inventory.py`:**

```python
# Add widget for workload B
dbutils.widgets.text("gcp_workload_b_storage_url", "", "GCP Workload B Storage URL")
gcp_workload_b_storage_url = dbutils.widgets.get("gcp_workload_b_storage_url")

# Update source_paths to include workload B
source_paths = {
    "gcp_workload_a": f"{gcp_workload_a_storage_url}asset-inventory/",
    "gcp_workload_b": f"{gcp_workload_b_storage_url}asset-inventory/",  # NEW
}
```

**`notebooks/bronze/gcp/04_scc_findings.py`:**

```python
# Add widget for workload B
dbutils.widgets.text("gcp_workload_b_storage_url", "", "GCP Workload B Storage URL")
gcp_workload_b_storage_url = dbutils.widgets.get("gcp_workload_b_storage_url")

# Update source_paths to include workload B
source_paths = {
    "gcp_workload_a": f"{gcp_workload_a_storage_url}scc-findings/",
    "gcp_workload_b": f"{gcp_workload_b_storage_url}scc-findings/",  # NEW
}
```

---

## 6. Apply Sequence

The multi-root apply follows this order:

```
Step 1: Initialize and apply the new workload root
        cd workloads/gcp-workload-b
        terraform init
        terraform plan    -> Verify ~15-20 new resources
        terraform apply
        -> Creates VPC, VMs, GCS bucket, Cloud Logging sinks,
           Asset Inventory export, service account IAM binding

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
        -> Cloud Audit Log sink starts immediately
        -> VPC Flow Logs may take 10-15 minutes
        -> Asset Inventory export runs on schedule
```

**Key difference from AWS onboarding:** No jobs module or hub/main.tf code changes are needed. The jobs module uses a dynamic `workloads` map that auto-generates parameters for any workload in the manifest. The cloud-integration module creates external locations via `for_each` over the same manifest.

---

## 7. Validation Checklist

After apply, verify each layer:

### GCP Infrastructure
```bash
# Verify VPC network exists
gcloud compute networks describe lakehouse-gcp-workload-b-vpc \
  --project=my-gcp-project-456

# Verify subnet exists
gcloud compute networks subnets describe lakehouse-gcp-workload-b-subnet \
  --project=my-gcp-project-456 --region=us-central1

# Verify GCS bucket exists
gcloud storage buckets describe gs://lakehouse-gcp-workload-b-security-logs-XXXX \
  --project=my-gcp-project-456

# Verify Cloud Logging sinks
gcloud logging sinks list --project=my-gcp-project-456

# Verify VMs are running
gcloud compute instances list --project=my-gcp-project-456
```

### Service Account Access
```bash
# Verify the service account has Storage Object Viewer on the bucket
gcloud storage buckets get-iam-policy \
  gs://lakehouse-gcp-workload-b-security-logs-XXXX \
  --project=my-gcp-project-456
```

### Databricks
```sql
-- Verify external location is accessible
LIST 'gs://lakehouse-gcp-workload-b-security-logs-XXXX/';

-- After data flows, verify bronze tables include new project data
SELECT DISTINCT _source_file
FROM security_poc.bronze.gcp_audit_log_raw
WHERE _source_file LIKE '%gcp-workload-b%';
```

---

## 8. Why No Foundation, Jobs, or Hub Code Changes Are Needed

The GCP expansion was designed with the same dynamic discovery patterns as Azure that eliminate per-workload code changes:

| Component | Why It Auto-Matches |
|---|---|
| **Service account** | Created once in `foundations/gcp-security/`. Each workload's data-sources module grants it `Storage Object Viewer` on the workload's GCS bucket. No changes to the foundation needed. |
| **Cloud integration** | Uses `for_each` over `var.workloads`. New workloads get external locations automatically. GCP workloads are routed to the GCP storage credential via `cloud == "gcp"` conditional. |
| **Jobs module** | Uses a dynamic `workloads` map: `{ for alias, w in var.workloads : "${alias}_storage_url" => w.storage_url }`. New workloads get job parameters automatically. |
| **Hub main.tf** | Passes `var.workloads` directly to cloud-integration and transforms it into the dynamic map for jobs. No per-workload `try()` blocks needed (same as the Azure pattern). |
| **Hub gcp_credentials** | Set once when the first GCP workload is added. All GCP workloads share the same service account. |

**The only non-dynamic pieces are the notebooks.** Notebook widget definitions and `source_paths` dictionaries are hardcoded per-workload. This is the only file modification needed when adding a new GCP project.

---

## 9. Scaling Notes

### Current Limitations

**Notebook storage URL parameters are positional.** The current notebooks use named widget parameters (`gcp_workload_a_storage_url`, `gcp_workload_b_storage_url`, etc.). At ~5+ projects, consider refactoring notebooks to accept a JSON-encoded list of storage URLs instead.

**External locations are per-workload.** Each workload gets one external location via `for_each`. At scale, this is manageable since external locations are lightweight Databricks metadata objects.

### Naming Convention

All resources follow the pattern `lakehouse-{alias}-{resource_type}`:

| Resource | Naming Pattern | Example |
|---|---|---|
| Workload root | `workloads/gcp-{alias}/` | `workloads/gcp-workload-b/` |
| VPC network | `lakehouse-gcp-{alias}-vpc` | `lakehouse-gcp-workload-b-vpc` |
| Subnet | `lakehouse-gcp-{alias}-subnet` | `lakehouse-gcp-workload-b-subnet` |
| GCS bucket | `lakehouse-gcp-{alias}-security-logs-XXXX` | `lakehouse-gcp-workload-b-security-logs-XXXX` |
| External Location | `security-logs-gcp-{alias}` | `security-logs-gcp-workload-b` |

The `workload_alias` must be unique and should follow the `gcp-workload-{letter}` convention.

### What If You Need to Remove a Workload Project?

1. Remove the workload root directory (`workloads/gcp-workload-b/`)
2. Remove the widget and source path from each notebook in `notebooks/bronze/gcp/`
3. Re-run `./scripts/assemble-workloads.sh` (removes the workload from the manifest)
4. Run `cd hub && terraform apply` -- Terraform will destroy the external location
5. Run `cd workloads/gcp-workload-b && terraform destroy` to remove GCP resources
6. **Caution:** This destroys the GCS bucket and all security logs in it. Consider backing up first.

---

## 10. Troubleshooting

### "Permission denied" when applying the workload root

The gcloud session needs access to the target project. Verify:

```bash
gcloud config set project my-gcp-project-456
gcloud projects describe my-gcp-project-456
```

### Required API not enabled

If Terraform fails with an API not enabled error:

```bash
gcloud services enable compute.googleapis.com --project=my-gcp-project-456
gcloud services enable logging.googleapis.com --project=my-gcp-project-456
gcloud services enable cloudasset.googleapis.com --project=my-gcp-project-456
# Only if using SCC:
gcloud services enable securitycenter.googleapis.com --project=my-gcp-project-456
```

### Service account IAM binding fails

If the service account hasn't been created yet, run `foundations/gcp-security/` first:

```bash
cd foundations/gcp-security && terraform apply
```

Then pass the `service_account_email` output to the workload's `terraform.tfvars`.

### Databricks external location validation fails

If the external location creation fails with a permissions error:
1. Verify the service account has `Storage Object Viewer` on the workload's GCS bucket
2. Verify `gcp_credentials` in hub terraform.tfvars matches the foundation outputs
3. Verify the GCP storage credential was created in Databricks: check `hub/` state for `databricks_storage_credential.gcp`

### Cloud Logging sink not exporting

Verify the sink was created and is not disabled:

```bash
gcloud logging sinks list --project=my-gcp-project-456
```

Check the sink's writer identity has permission to write to the GCS bucket:

```bash
gcloud logging sinks describe <sink-name> --project=my-gcp-project-456
```

### VPC Flow Logs not appearing

VPC Flow Logs require the subnet to have flow logging enabled. Verify:

```bash
gcloud compute networks subnets describe lakehouse-gcp-workload-b-subnet \
  --project=my-gcp-project-456 --region=us-central1 \
  --format="value(logConfig)"
```

### `assemble-workloads.sh` skips the new workload

The workload root must be initialized and applied before `assemble-workloads.sh` can collect its outputs. Run `terraform init && terraform apply` in `workloads/gcp-<alias>/` first.

### "empty directory" in Auto Loader notebook

This is normal -- it means no files exist yet at the GCS path. Wait 30 minutes for data to flow. The notebooks handle this gracefully with a try/except.

---

## 11. Quick Reference: Files Changed per New Project

### New files (created from template)

| File | Contents |
|---|---|
| `workloads/gcp-<alias>/` | Full workload root (copied from `_template-gcp/`) |
| `workloads/gcp-<alias>/terraform.tfvars` | Project-specific values |
| `workloads/gcp-<alias>/backend.tf` | S3 backend with unique state key |

### Modified files

| File | Change Type | What to Add |
|---|---|---|
| `notebooks/bronze/gcp/01_cloud_audit_logs.py` | Update paths | Add widget + `source_paths` entry |
| `notebooks/bronze/gcp/02_vpc_flow_logs.py` | Update paths | Add widget + `source_paths` entry |
| `notebooks/bronze/gcp/03_asset_inventory.py` | Update paths | Add widget + `source_paths` entry |
| `notebooks/bronze/gcp/04_scc_findings.py` | Update paths | Add widget + `source_paths` entry |

### Auto-generated files (no manual changes)

| File | How it's populated |
|---|---|
| `hub/workloads.auto.tfvars.json` | Generated by `assemble-workloads.sh` from workload outputs |

**Total: 1 new workload root + 4 modified notebooks, ~15-20 new GCP resources, 1 new Databricks external location, 4 updated Databricks jobs.**

**No changes needed:** GCP security foundation, cloud-integration module (uses `for_each`), jobs module (dynamic workloads map), hub/main.tf, unity-catalog module, workspace-config module, hub gcp_credentials (set once).

### Comparison with AWS and Azure Onboarding

| Aspect | AWS | Azure | GCP |
|---|---|---|---|
| Files modified per account | 7 | 2 | 4 |
| Jobs module changes | Yes (per-workload variables) | No (dynamic map) | No (dynamic map) |
| Hub main.tf changes | Yes (per-workload `try()` blocks) | No (dynamic `for_each`) | No (dynamic `for_each`) |
| Foundation changes | None | None | None |
| Notebook changes | 4 notebooks | 2 notebooks | 4 notebooks |
| Cloud resources created | ~27 | ~20 | ~15-20 |
| State backend | S3 (AWS) | S3 (AWS) -- shared backend | S3 (AWS) -- shared backend |
