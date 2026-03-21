#!/usr/bin/env bash
# =============================================================================
# onboard_gcp_workload.sh — Onboard a new GCP workload project
# =============================================================================
# Creates a new workload root from the GCP template and updates the GCP bronze
# notebooks to include the new workload's storage URL. Does NOT run
# terraform apply.
#
# Like the Azure script, this does NOT modify the jobs module or hub/main.tf
# because the GCP expansion uses a dynamic workloads map (for_each) that
# auto-discovers new workloads via assemble-workloads.sh.
#
# Usage:
#   ./onboard_gcp_workload.sh \
#     --alias workload-b \
#     --project-id my-gcp-project-456 \
#     --service-account-email lakehouse-sa@my-security-proj.iam.gserviceaccount.com \
#     --security-account-id <SECURITY_ACCOUNT_ID> \
#     --vpc-cidr 10.21.0.0/24 \
#     --region us-central1 \
#     --zone us-central1-a
#
# Prerequisites:
#   - GCP project exists and you are authenticated (gcloud auth application-default login)
#   - foundations/gcp-security/ has been applied (creates the service account)
#   - The service_account_email comes from: cd foundations/gcp-security && terraform output service_account_email
#   - Choose a VPC subnet CIDR that does not overlap with existing workloads:
#       gcp-workload-a = 10.20.0.0/24, gcp-workload-b = 10.21.0.0/24
#
# What this script does:
#   1. Copies workloads/_template-gcp/ → workloads/gcp-<alias>/
#   2. Creates terraform.tfvars with project-specific values
#   3. Creates backend.tf with the correct state key
#   4. Adds widget + source path to each GCP bronze notebook
#
# What this script does NOT do (handled automatically):
#   - Modify the jobs module (dynamic workloads map handles new workloads)
#   - Modify hub/main.tf (dynamic for_each handles new workloads)
#   - Modify the cloud-integration module (for_each over workloads list)
#   - Modify the security foundation (service account is shared)
#
# After running this script:
#   cd workloads/gcp-<alias>
#   terraform init && terraform apply
#   cd ../.. && ./scripts/assemble-workloads.sh
#   cd hub && terraform apply
# =============================================================================

set -euo pipefail

# ── Repo root detection ──────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

# Verify we're in the right repo
if [[ ! -d "$REPO_ROOT/workloads/_template-gcp" ]] || [[ ! -d "$REPO_ROOT/hub" ]]; then
  echo "ERROR: Script must be run from the repository root (databricks-security-lakehouse/)."
  echo "       Expected directories: workloads/_template-gcp/, hub/"
  exit 1
fi

# ── Argument parsing ─────────────────────────────────────────────────────────
ALIAS=""
PROJECT_ID=""
SERVICE_ACCOUNT_EMAIL=""
SECURITY_ACCOUNT_ID=""
VPC_CIDR=""
REGION="us-central1"
ZONE="us-central1-a"
ENABLE_SCC="false"

usage() {
  cat <<'USAGE'
Usage: onboard_gcp_workload.sh --alias <name> --project-id <id> \
         --service-account-email <email> --security-account-id <12-digit> \
         --vpc-cidr <cidr> [--region <region>] [--zone <zone>] [--enable-scc]

Required arguments:
  --alias               Workload alias (e.g., workload-b). Used in all resource
                        naming. Must be lowercase, alphanumeric + hyphens.
                        The script prefixes "gcp-" for the directory and
                        workload_alias (e.g., gcp-workload-b).
  --project-id          GCP project ID (e.g., my-gcp-project-456).
  --service-account-email  Email of the Databricks service account from
                        foundations/gcp-security/ output.
  --security-account-id 12-digit AWS account ID for the security account
                        (used for the S3 state backend bucket name).
  --vpc-cidr            VPC subnet CIDR block (e.g., 10.21.0.0/24). Must not
                        overlap with existing workloads.

Optional arguments:
  --region              GCP region (default: us-central1).
  --zone                GCP zone (default: us-central1-a). Must be within the
                        specified region.
  --enable-scc          Enable SCC Findings export (default: false).
  -h / --help           Print usage information and exit.

Example:
  ./onboard_gcp_workload.sh \
    --alias workload-b \
    --project-id my-gcp-project-456 \
    --service-account-email lakehouse-sa@my-security-proj.iam.gserviceaccount.com \
    --security-account-id <SECURITY_ACCOUNT_ID> \
    --vpc-cidr 10.21.0.0/24 \
    --region us-central1 \
    --zone us-central1-a
USAGE
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --alias)                 ALIAS="$2"; shift 2 ;;
    --project-id)            PROJECT_ID="$2"; shift 2 ;;
    --service-account-email) SERVICE_ACCOUNT_EMAIL="$2"; shift 2 ;;
    --security-account-id)   SECURITY_ACCOUNT_ID="$2"; shift 2 ;;
    --vpc-cidr)              VPC_CIDR="$2"; shift 2 ;;
    --region)                REGION="$2"; shift 2 ;;
    --zone)                  ZONE="$2"; shift 2 ;;
    --enable-scc)            ENABLE_SCC="true"; shift ;;
    -h|--help)               usage ;;
    *)                       echo "ERROR: Unknown argument: $1"; usage ;;
  esac
done

# ── Validation ───────────────────────────────────────────────────────────────
errors=()

if [[ -z "$ALIAS" ]]; then
  errors+=("--alias is required")
elif ! [[ "$ALIAS" =~ ^[a-z][a-z0-9-]+$ ]]; then
  errors+=("--alias must be lowercase alphanumeric with hyphens (got: $ALIAS)")
fi

if [[ -z "$PROJECT_ID" ]]; then
  errors+=("--project-id is required")
elif ! [[ "$PROJECT_ID" =~ ^[a-z][a-z0-9-]+$ ]]; then
  errors+=("--project-id must be lowercase alphanumeric with hyphens (got: $PROJECT_ID)")
fi

if [[ -z "$SERVICE_ACCOUNT_EMAIL" ]]; then
  errors+=("--service-account-email is required")
elif ! [[ "$SERVICE_ACCOUNT_EMAIL" =~ ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]]; then
  errors+=("--service-account-email must be a valid email address (got: $SERVICE_ACCOUNT_EMAIL)")
fi

if [[ -z "$SECURITY_ACCOUNT_ID" ]]; then
  errors+=("--security-account-id is required")
elif ! [[ "$SECURITY_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  errors+=("--security-account-id must be exactly 12 digits (got: $SECURITY_ACCOUNT_ID)")
fi

if [[ -z "$VPC_CIDR" ]]; then
  errors+=("--vpc-cidr is required")
elif ! [[ "$VPC_CIDR" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ ]]; then
  errors+=("--vpc-cidr must be a valid CIDR (got: $VPC_CIDR)")
fi

if [[ ${#errors[@]} -gt 0 ]]; then
  echo "ERROR: Validation failed:"
  for e in "${errors[@]}"; do echo "  - $e"; done
  echo ""
  usage
fi

# Derive naming variants:
#   ALIAS_FULL      = "gcp-workload-b"  (directory name + workload_alias in tfvars)
#   ALIAS_UNDERSCORE = "gcp_workload_b" (used in notebook widget/variable names)
ALIAS_FULL="gcp-${ALIAS}"
ALIAS_UNDERSCORE="${ALIAS_FULL//-/_}"

# The workload root directory name
WORKLOAD_DIR="$REPO_ROOT/workloads/${ALIAS_FULL}"

# ── Idempotency check ───────────────────────────────────────────────────────
if [[ -d "$WORKLOAD_DIR" ]]; then
  echo "ERROR: Workload directory already exists: workloads/${ALIAS_FULL}"
  echo "       This alias appears to be already onboarded."
  exit 1
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo "============================================================"
echo "  Onboarding new GCP workload project"
echo "============================================================"
echo "  Alias:                $ALIAS"
echo "  Full alias:           $ALIAS_FULL"
echo "  Project ID:           $PROJECT_ID"
echo "  Service Account:      $SERVICE_ACCOUNT_EMAIL"
echo "  Security Account:     $SECURITY_ACCOUNT_ID"
echo "  Region:               $REGION"
echo "  Zone:                 $ZONE"
echo "  VPC CIDR:             $VPC_CIDR"
echo "  Enable SCC:           $ENABLE_SCC"
echo "  Workload root:        workloads/${ALIAS_FULL}/"
echo "  Notebook var prefix:  ${ALIAS_UNDERSCORE}"
echo "============================================================"
echo ""

STEP=0
modified_files=()

step() {
  STEP=$((STEP + 1))
  echo "[$STEP] $1"
}

# =============================================================================
# 1. Copy template to new workload root
# =============================================================================
step "Copying workloads/_template-gcp/ → workloads/${ALIAS_FULL}/"

cp -r "$REPO_ROOT/workloads/_template-gcp" "$WORKLOAD_DIR"
# Remove the example files — we'll create real ones
rm -f "$WORKLOAD_DIR/backend.tf.example"
rm -f "$WORKLOAD_DIR/terraform.tfvars.example"

# =============================================================================
# 2. Create terraform.tfvars
# =============================================================================
step "workloads/${ALIAS_FULL}/terraform.tfvars — creating with project values"

cat > "$WORKLOAD_DIR/terraform.tfvars" <<EOF
project_id            = "${PROJECT_ID}"
region                = "${REGION}"
zone                  = "${ZONE}"
workload_alias        = "${ALIAS_FULL}"
vpc_cidr              = "${VPC_CIDR}"
name_prefix           = "lakehouse"
service_account_email = "${SERVICE_ACCOUNT_EMAIL}"
enable_scc            = ${ENABLE_SCC}
EOF
modified_files+=("workloads/${ALIAS_FULL}/terraform.tfvars")

# =============================================================================
# 3. Create backend.tf
# =============================================================================
step "workloads/${ALIAS_FULL}/backend.tf — creating with state key"

cat > "$WORKLOAD_DIR/backend.tf" <<EOF
terraform {
  backend "s3" {
    bucket         = "security-lakehouse-tfstate-${SECURITY_ACCOUNT_ID}"
    key            = "workloads/${ALIAS_FULL}/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "security-lakehouse-tflock"
    encrypt        = true
  }
}
EOF
modified_files+=("workloads/${ALIAS_FULL}/backend.tf")

# =============================================================================
# 4. Notebooks — Add widget + source path for each GCP data source
# =============================================================================
# The GCP bronze notebooks need a widget and source_paths entry for each
# workload project. Like the Azure onboarding script, no changes are needed
# for the jobs module or hub/main.tf (dynamic workloads map).

NOTEBOOK_FILES=(
  "01_cloud_audit_logs.py"
  "02_vpc_flow_logs.py"
  "03_asset_inventory.py"
  "04_scc_findings.py"
)
NOTEBOOK_PATH_PREFIXES=(
  "cloudaudit.googleapis.com/"
  "compute.googleapis.com%2Fvpc_flows/"
  "asset-inventory/"
  "scc-findings/"
)

# Derive the display label for notebook widgets (workload-b → GCP Workload B)
DISPLAY_LABEL="GCP $(echo "$ALIAS" | sed 's/-/ /g' | awk '{for(i=1;i<=NF;i++) $i=toupper(substr($i,1,1)) substr($i,2)}1')"

for i in "${!NOTEBOOK_FILES[@]}"; do
  notebook="${NOTEBOOK_FILES[$i]}"
  path_prefix="${NOTEBOOK_PATH_PREFIXES[$i]}"
  NOTEBOOK_FILE="$REPO_ROOT/notebooks/bronze/gcp/$notebook"

  if [[ ! -f "$NOTEBOOK_FILE" ]]; then
    echo "  WARNING: $notebook not found, skipping"
    continue
  fi

  step "notebooks/bronze/gcp/$notebook — adding ${ALIAS_UNDERSCORE} widget and source path"

  # 1. Add widget definition after the last gcp_workload widget line,
  #    before the checkpoint_base widget.
  sed -i.bak '/dbutils\.widgets\.text("checkpoint_base"/i\
dbutils.widgets.text("'"${ALIAS_UNDERSCORE}"'_storage_url", "", "'"${DISPLAY_LABEL}"' Storage URL")' "$NOTEBOOK_FILE"
  rm -f "${NOTEBOOK_FILE}.bak"

  # 2. Add variable assignment after the last gcp_workload variable,
  #    before the checkpoint_base variable.
  sed -i.bak '/^checkpoint_base = dbutils\.widgets\.get("checkpoint_base")/i\
'"${ALIAS_UNDERSCORE}"'_storage_url = dbutils.widgets.get("'"${ALIAS_UNDERSCORE}"'_storage_url")' "$NOTEBOOK_FILE"
  rm -f "${NOTEBOOK_FILE}.bak"

  # 3. Add entry in source_paths dict after the last gcp_workload entry.
  #    Use a temp file approach to avoid sed delimiter conflicts with gs://.
  NEW_PATH_LINE='    "'"${ALIAS_UNDERSCORE}"'": f"{'"${ALIAS_UNDERSCORE}"'_storage_url}'"${path_prefix}"'",'
  {
    found=0
    while IFS= read -r line; do
      printf '%s\n' "$line"
      if [[ $found -eq 0 ]] && echo "$line" | grep -q '"gcp_workload_.*_storage_url'; then
        printf '%s\n' "$NEW_PATH_LINE"
        found=1
      fi
    done < "$NOTEBOOK_FILE"
  } > "${NOTEBOOK_FILE}.tmp"
  mv "${NOTEBOOK_FILE}.tmp" "$NOTEBOOK_FILE"

  modified_files+=("notebooks/bronze/gcp/$notebook")
done

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "============================================================"
echo "  Onboarding complete — ${#modified_files[@]} files created/modified"
echo "============================================================"
echo ""
echo "Created/modified files:"
for f in "${modified_files[@]}"; do
  echo "  $f"
done
echo ""
echo "New workload root: workloads/${ALIAS_FULL}/"
echo "  (copied from workloads/_template-gcp/ with terraform.tfvars and backend.tf)"
echo ""
echo "Next steps:"
echo "  1. Review changes:          git diff"
echo "  2. Init + apply workload:   cd workloads/${ALIAS_FULL} && terraform init && terraform apply"
echo "  3. Assemble workloads:      cd ../.. && ./scripts/assemble-workloads.sh"
echo "  4. Apply hub:               cd hub && terraform apply"
echo "  5. Format Terraform:        terraform fmt -recursive ."
echo ""
echo "See onboarding_new_gcp_accounts.md for detailed apply sequence and validation."
