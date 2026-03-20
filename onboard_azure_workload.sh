#!/usr/bin/env bash
# =============================================================================
# onboard_azure_workload.sh — Onboard a new Azure workload subscription
# =============================================================================
# Creates a new workload root from the Azure template and updates the Azure
# bronze notebooks to include the new workload's storage URL. Does NOT run
# terraform apply.
#
# Unlike the AWS script, this does NOT modify the jobs module or hub/main.tf
# because the Azure expansion uses a dynamic workloads map (for_each) that
# auto-discovers new workloads via assemble-workloads.sh.
#
# Usage:
#   ./onboard_azure_workload.sh \
#     --alias workload-b \
#     --subscription-id 12345678-1234-1234-1234-123456789abc \
#     --service-principal-id 87654321-4321-4321-4321-cba987654321 \
#     --security-account-id <SECURITY_ACCOUNT_ID> \
#     --vnet-cidr 10.11.0.0/16 \
#     --subnet-cidr 10.11.1.0/24
#
# Prerequisites:
#   - Azure subscription exists and you are authenticated (az login)
#   - foundations/azure-security/ has been applied (creates the service principal)
#   - The service_principal_id comes from: cd foundations/azure-security && terraform output service_principal_id
#   - Choose a VNet CIDR that does not overlap with existing workloads:
#       azure-workload-a = 10.10.0.0/16, azure-workload-b = 10.11.0.0/16
#
# What this script does:
#   1. Copies workloads/_template-azure/ → workloads/azure-<alias>/
#   2. Creates terraform.tfvars with subscription-specific values
#   3. Creates backend.tf with the correct state key
#   4. Adds widget + source path to each Azure bronze notebook
#
# What this script does NOT do (handled automatically):
#   - Modify the jobs module (dynamic workloads map handles new workloads)
#   - Modify hub/main.tf (dynamic for_each handles new workloads)
#   - Modify the cloud-integration module (for_each over workloads list)
#   - Modify the security foundation (service principal is shared)
#
# After running this script:
#   cd workloads/azure-<alias>
#   terraform init && terraform apply
#   cd ../.. && ./scripts/assemble-workloads.sh
#   cd hub && terraform apply
# =============================================================================

set -euo pipefail

# ── Repo root detection ──────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

# Verify we're in the right repo
if [[ ! -d "$REPO_ROOT/workloads/_template-azure" ]] || [[ ! -d "$REPO_ROOT/hub" ]]; then
  echo "ERROR: Script must be run from the repository root (databricks-security-lakehouse/)."
  echo "       Expected directories: workloads/_template-azure/, hub/"
  exit 1
fi

# ── Argument parsing ─────────────────────────────────────────────────────────
ALIAS=""
SUBSCRIPTION_ID=""
SERVICE_PRINCIPAL_ID=""
SECURITY_ACCOUNT_ID=""
VNET_CIDR=""
SUBNET_CIDR=""
LOCATION="eastus"

usage() {
  cat <<'USAGE'
Usage: onboard_azure_workload.sh --alias <name> --subscription-id <uuid> \
         --service-principal-id <uuid> --security-account-id <12-digit> \
         --vnet-cidr <cidr> --subnet-cidr <cidr> [--location <region>]

Required arguments:
  --alias               Workload alias (e.g., workload-b). Used in all resource
                        naming. Must be lowercase, alphanumeric + hyphens.
                        The script prefixes "azure-" for the directory and
                        workload_alias (e.g., azure-workload-b).
  --subscription-id     Azure subscription ID (UUID format).
  --service-principal-id  Object ID of the Entra ID service principal from
                        foundations/azure-security/ output.
  --security-account-id 12-digit AWS account ID for the security account
                        (used for the S3 state backend bucket name).
  --vnet-cidr           VNet CIDR block (e.g., 10.11.0.0/16). Must not overlap.
  --subnet-cidr         Subnet CIDR (e.g., 10.11.1.0/24). Must be within vnet-cidr.

Optional arguments:
  --location            Azure region (default: eastus).
  -h / --help           Print usage information and exit.

Example:
  ./onboard_azure_workload.sh \
    --alias workload-b \
    --subscription-id 12345678-1234-1234-1234-123456789abc \
    --service-principal-id 87654321-4321-4321-4321-cba987654321 \
    --security-account-id <SECURITY_ACCOUNT_ID> \
    --vnet-cidr 10.11.0.0/16 \
    --subnet-cidr 10.11.1.0/24
USAGE
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --alias)                ALIAS="$2"; shift 2 ;;
    --subscription-id)      SUBSCRIPTION_ID="$2"; shift 2 ;;
    --service-principal-id) SERVICE_PRINCIPAL_ID="$2"; shift 2 ;;
    --security-account-id)  SECURITY_ACCOUNT_ID="$2"; shift 2 ;;
    --vnet-cidr)            VNET_CIDR="$2"; shift 2 ;;
    --subnet-cidr)          SUBNET_CIDR="$2"; shift 2 ;;
    --location)             LOCATION="$2"; shift 2 ;;
    -h|--help)              usage ;;
    *)                      echo "ERROR: Unknown argument: $1"; usage ;;
  esac
done

# ── Validation ───────────────────────────────────────────────────────────────
errors=()

if [[ -z "$ALIAS" ]]; then
  errors+=("--alias is required")
elif ! [[ "$ALIAS" =~ ^[a-z][a-z0-9-]+$ ]]; then
  errors+=("--alias must be lowercase alphanumeric with hyphens (got: $ALIAS)")
fi

if [[ -z "$SUBSCRIPTION_ID" ]]; then
  errors+=("--subscription-id is required")
elif ! [[ "$SUBSCRIPTION_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
  errors+=("--subscription-id must be a UUID (got: $SUBSCRIPTION_ID)")
fi

if [[ -z "$SERVICE_PRINCIPAL_ID" ]]; then
  errors+=("--service-principal-id is required")
elif ! [[ "$SERVICE_PRINCIPAL_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
  errors+=("--service-principal-id must be a UUID (got: $SERVICE_PRINCIPAL_ID)")
fi

if [[ -z "$SECURITY_ACCOUNT_ID" ]]; then
  errors+=("--security-account-id is required")
elif ! [[ "$SECURITY_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  errors+=("--security-account-id must be exactly 12 digits (got: $SECURITY_ACCOUNT_ID)")
fi

if [[ -z "$VNET_CIDR" ]]; then
  errors+=("--vnet-cidr is required")
elif ! [[ "$VNET_CIDR" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ ]]; then
  errors+=("--vnet-cidr must be a valid CIDR (got: $VNET_CIDR)")
fi

if [[ -z "$SUBNET_CIDR" ]]; then
  errors+=("--subnet-cidr is required")
elif ! [[ "$SUBNET_CIDR" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ ]]; then
  errors+=("--subnet-cidr must be a valid CIDR (got: $SUBNET_CIDR)")
fi

if [[ ${#errors[@]} -gt 0 ]]; then
  echo "ERROR: Validation failed:"
  for e in "${errors[@]}"; do echo "  - $e"; done
  echo ""
  usage
fi

# Derive naming variants:
#   ALIAS_FULL      = "azure-workload-b"  (directory name + workload_alias in tfvars)
#   ALIAS_UNDERSCORE = "azure_workload_b" (used in notebook widget/variable names)
ALIAS_FULL="azure-${ALIAS}"
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
echo "  Onboarding new Azure workload subscription"
echo "============================================================"
echo "  Alias:                $ALIAS"
echo "  Full alias:           $ALIAS_FULL"
echo "  Subscription ID:      $SUBSCRIPTION_ID"
echo "  Service Principal ID: $SERVICE_PRINCIPAL_ID"
echo "  Security Account:     $SECURITY_ACCOUNT_ID"
echo "  Location:             $LOCATION"
echo "  VNet CIDR:            $VNET_CIDR"
echo "  Subnet CIDR:          $SUBNET_CIDR"
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
step "Copying workloads/_template-azure/ → workloads/${ALIAS_FULL}/"

cp -r "$REPO_ROOT/workloads/_template-azure" "$WORKLOAD_DIR"
# Remove the example files — we'll create real ones
rm -f "$WORKLOAD_DIR/backend.tf.example"

# =============================================================================
# 2. Create terraform.tfvars
# =============================================================================
step "workloads/${ALIAS_FULL}/terraform.tfvars — creating with subscription values"

cat > "$WORKLOAD_DIR/terraform.tfvars" <<EOF
subscription_id      = "${SUBSCRIPTION_ID}"
location             = "${LOCATION}"
workload_alias       = "${ALIAS_FULL}"
vnet_cidr            = "${VNET_CIDR}"
subnet_cidr          = "${SUBNET_CIDR}"
name_prefix          = "lakehouse"
service_principal_id = "${SERVICE_PRINCIPAL_ID}"
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
# 4. Notebooks — Add widget + source path for each Azure data source
# =============================================================================
# The Azure bronze notebooks need a widget and source_paths entry for each
# workload subscription. Unlike the AWS onboarding script, no changes are
# needed for the jobs module or hub/main.tf (dynamic workloads map).

NOTEBOOK_FILES=(
  "01_activity_log.py"
  "02_vnet_flow.py"
)
NOTEBOOK_PATH_PREFIXES=(
  "insights-activity-logs/"
  "vnet-flow-logs/"
)

# Derive the display label for notebook widgets (workload-b → Azure Workload B)
DISPLAY_LABEL="Azure $(echo "$ALIAS" | sed 's/-/ /g' | awk '{for(i=1;i<=NF;i++) $i=toupper(substr($i,1,1)) substr($i,2)}1')"

for i in "${!NOTEBOOK_FILES[@]}"; do
  notebook="${NOTEBOOK_FILES[$i]}"
  path_prefix="${NOTEBOOK_PATH_PREFIXES[$i]}"
  NOTEBOOK_FILE="$REPO_ROOT/notebooks/bronze/azure/$notebook"

  if [[ ! -f "$NOTEBOOK_FILE" ]]; then
    echo "  WARNING: $notebook not found, skipping"
    continue
  fi

  step "notebooks/bronze/azure/$notebook — adding ${ALIAS_UNDERSCORE} widget and source path"

  # 1. Add widget definition after the last azure_workload widget line,
  #    before the checkpoint_base widget.
  sed -i.bak '/dbutils\.widgets\.text("checkpoint_base"/i\
dbutils.widgets.text("'"${ALIAS_UNDERSCORE}"'_storage_url", "", "'"${DISPLAY_LABEL}"' Storage URL")' "$NOTEBOOK_FILE"
  rm -f "${NOTEBOOK_FILE}.bak"

  # 2. Add variable assignment after the last azure_workload variable,
  #    before the checkpoint_base variable.
  sed -i.bak '/^checkpoint_base = dbutils\.widgets\.get("checkpoint_base")/i\
'"${ALIAS_UNDERSCORE}"'_storage_url = dbutils.widgets.get("'"${ALIAS_UNDERSCORE}"'_storage_url")' "$NOTEBOOK_FILE"
  rm -f "${NOTEBOOK_FILE}.bak"

  # 3. Add entry in source_paths dict after the last azure_workload entry.
  #    Use a temp file approach to avoid sed delimiter conflicts with abfss://.
  NEW_PATH_LINE='    "'"${ALIAS_UNDERSCORE}"'": f"{'"${ALIAS_UNDERSCORE}"'_storage_url}'"${path_prefix}"'",'
  {
    found=0
    while IFS= read -r line; do
      printf '%s\n' "$line"
      if [[ $found -eq 0 ]] && echo "$line" | grep -q '"azure_workload_.*_storage_url'; then
        printf '%s\n' "$NEW_PATH_LINE"
        found=1
      fi
    done < "$NOTEBOOK_FILE"
  } > "${NOTEBOOK_FILE}.tmp"
  mv "${NOTEBOOK_FILE}.tmp" "$NOTEBOOK_FILE"

  modified_files+=("notebooks/bronze/azure/$notebook")
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
echo "  (copied from workloads/_template-azure/ with terraform.tfvars and backend.tf)"
echo ""
echo "Next steps:"
echo "  1. Review changes:          git diff"
echo "  2. Init + apply workload:   cd workloads/${ALIAS_FULL} && terraform init && terraform apply"
echo "  3. Assemble workloads:      cd ../.. && ./scripts/assemble-workloads.sh"
echo "  4. Apply hub:               cd hub && terraform apply"
echo "  5. Format Terraform:        terraform fmt -recursive ."
echo ""
echo "See onboarding_new_azure_accounts.md for detailed apply sequence and validation."
