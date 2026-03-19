#!/usr/bin/env bash
# =============================================================================
# onboard_workload_account.sh — Onboard a new AWS workload account
# =============================================================================
# Creates a new workload root from the template and updates the hub/jobs module
# to include the new workload's bucket. Does NOT run terraform apply.
#
# Usage:
#   ./onboard_workload_account.sh \
#     --alias workload-c \
#     --account-id 123456789012 \
#     --security-account-id <SECURITY_ACCOUNT_ID> \
#     --vpc-cidr 10.2.0.0/16 \
#     --subnet-cidr 10.2.1.0/24
#
# Prerequisites:
#   - The account must be an AWS Organizations member with
#     OrganizationAccountAccessRole available.
#   - Choose a VPC CIDR that does not overlap with existing workloads:
#       workload-a = 10.0.0.0/16, workload-b = 10.1.0.0/16
#
# What this script does:
#   1. Copies workloads/_template-aws/ → workloads/aws-<alias>/
#   2. Creates terraform.tfvars with account-specific values
#   3. Creates backend.tf with the correct state key
#   4. Adds a bucket variable to modules/databricks/jobs/variables.tf
#   5. Adds the bucket to common_params in modules/databricks/jobs/main.tf
#   6. Adds the bucket extraction to hub/main.tf jobs module block
#   7. Adds widget + source path to each bronze notebook
#
# After running this script:
#   cd workloads/aws-<alias>
#   terraform init && terraform apply
#   cd ../.. && ./scripts/assemble-workloads.sh
#   cd hub && terraform apply
# =============================================================================

set -euo pipefail

# ── Repo root detection ──────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

# Verify we're in the right repo
if [[ ! -d "$REPO_ROOT/workloads/_template-aws" ]] || [[ ! -d "$REPO_ROOT/hub" ]]; then
  echo "ERROR: Script must be run from the repository root (databricks-security-lakehouse/)."
  echo "       Expected directories: workloads/_template-aws/, hub/"
  exit 1
fi

# ── Argument parsing ─────────────────────────────────────────────────────────
ALIAS=""
ACCOUNT_ID=""
SECURITY_ACCOUNT_ID=""
VPC_CIDR=""
SUBNET_CIDR=""

usage() {
  cat <<'USAGE'
Usage: onboard_workload_account.sh --alias <name> --account-id <12-digit> \
         --security-account-id <12-digit> --vpc-cidr <cidr> --subnet-cidr <cidr>

Required arguments:
  --alias               Workload account alias (e.g., workload-c). Used in all
                        resource naming. Must be lowercase, alphanumeric + hyphens.
  --account-id          12-digit AWS account ID for the new workload account.
  --security-account-id 12-digit AWS account ID for the security/management account.
  --vpc-cidr            VPC CIDR block (e.g., 10.2.0.0/16). Must not overlap existing.
  --subnet-cidr         Public subnet CIDR (e.g., 10.2.1.0/24). Must be within vpc-cidr.

Example:
  ./onboard_workload_account.sh \
    --alias workload-c \
    --account-id 123456789012 \
    --security-account-id <SECURITY_ACCOUNT_ID> \
    --vpc-cidr 10.2.0.0/16 \
    --subnet-cidr 10.2.1.0/24
USAGE
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --alias)               ALIAS="$2"; shift 2 ;;
    --account-id)          ACCOUNT_ID="$2"; shift 2 ;;
    --security-account-id) SECURITY_ACCOUNT_ID="$2"; shift 2 ;;
    --vpc-cidr)            VPC_CIDR="$2"; shift 2 ;;
    --subnet-cidr)         SUBNET_CIDR="$2"; shift 2 ;;
    -h|--help)             usage ;;
    *)                     echo "ERROR: Unknown argument: $1"; usage ;;
  esac
done

# ── Validation ───────────────────────────────────────────────────────────────
errors=()

if [[ -z "$ALIAS" ]]; then
  errors+=("--alias is required")
elif ! [[ "$ALIAS" =~ ^[a-z][a-z0-9-]+$ ]]; then
  errors+=("--alias must be lowercase alphanumeric with hyphens (got: $ALIAS)")
fi

if [[ -z "$ACCOUNT_ID" ]]; then
  errors+=("--account-id is required")
elif ! [[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  errors+=("--account-id must be exactly 12 digits (got: $ACCOUNT_ID)")
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

# Derive the underscore variant used in Terraform identifiers (workload-c → workload_c)
ALIAS_UNDERSCORE="${ALIAS//-/_}"

# The workload root directory name
WORKLOAD_DIR="$REPO_ROOT/workloads/aws-${ALIAS}"

# ── Idempotency check ───────────────────────────────────────────────────────
if [[ -d "$WORKLOAD_DIR" ]]; then
  echo "ERROR: Workload directory already exists: workloads/aws-${ALIAS}"
  echo "       This alias appears to be already onboarded."
  exit 1
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo "============================================================"
echo "  Onboarding new workload account"
echo "============================================================"
echo "  Alias:              $ALIAS"
echo "  Account ID:         $ACCOUNT_ID"
echo "  Security Account:   $SECURITY_ACCOUNT_ID"
echo "  VPC CIDR:           $VPC_CIDR"
echo "  Subnet CIDR:        $SUBNET_CIDR"
echo "  Workload root:      workloads/aws-${ALIAS}/"
echo "  TF variable prefix: ${ALIAS_UNDERSCORE}"
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
step "Copying workloads/_template-aws/ → workloads/aws-${ALIAS}/"

cp -r "$REPO_ROOT/workloads/_template-aws" "$WORKLOAD_DIR"
# Remove the example files — we'll create real ones
rm -f "$WORKLOAD_DIR/terraform.tfvars.example"
rm -f "$WORKLOAD_DIR/backend.tf.example"

# =============================================================================
# 2. Create terraform.tfvars
# =============================================================================
step "workloads/aws-${ALIAS}/terraform.tfvars — creating with account values"

cat > "$WORKLOAD_DIR/terraform.tfvars" <<EOF
aws_region          = "us-east-1"
account_alias       = "${ALIAS}"
account_id          = "${ACCOUNT_ID}"
vpc_cidr            = "${VPC_CIDR}"
public_subnet_cidr  = "${SUBNET_CIDR}"
security_account_id = "${SECURITY_ACCOUNT_ID}"
EOF
modified_files+=("workloads/aws-${ALIAS}/terraform.tfvars")

# =============================================================================
# 3. Create backend.tf
# =============================================================================
step "workloads/aws-${ALIAS}/backend.tf — creating with state key"

cat > "$WORKLOAD_DIR/backend.tf" <<EOF
terraform {
  backend "s3" {
    bucket         = "security-lakehouse-tfstate-${SECURITY_ACCOUNT_ID}"
    key            = "workloads/${ALIAS}/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "security-lakehouse-tflock"
    encrypt        = true
  }
}
EOF
modified_files+=("workloads/aws-${ALIAS}/backend.tf")

# =============================================================================
# 4. modules/databricks/jobs/variables.tf — Add bucket variable
# =============================================================================
step "modules/databricks/jobs/variables.tf — adding ${ALIAS_UNDERSCORE}_security_logs_bucket_name"

TARGET="$REPO_ROOT/modules/databricks/jobs/variables.tf"

sed -i.bak '/^variable "notebook_source_dir"/i\
variable "'"${ALIAS_UNDERSCORE}"'_security_logs_bucket_name" {\
  description = "'"${ALIAS}"' security logs S3 bucket name — source for Auto Loader"\
  type        = string\
}\
' "$TARGET"
rm -f "${TARGET}.bak"
modified_files+=("modules/databricks/jobs/variables.tf")

# =============================================================================
# 5. modules/databricks/jobs/main.tf — Update common_params
# =============================================================================
step "modules/databricks/jobs/main.tf — adding ${ALIAS_UNDERSCORE}_bucket to common_params"

TARGET="$REPO_ROOT/modules/databricks/jobs/main.tf"

sed -i.bak '/workload_b_bucket = var\.workload_b_security_logs_bucket_name/a\
    '"${ALIAS_UNDERSCORE}"'_bucket = var.'"${ALIAS_UNDERSCORE}"'_security_logs_bucket_name' "$TARGET"
rm -f "${TARGET}.bak"
modified_files+=("modules/databricks/jobs/main.tf")

# =============================================================================
# 6. hub/main.tf — Add bucket extraction for the jobs module
# =============================================================================
step "hub/main.tf — adding ${ALIAS_UNDERSCORE} bucket extraction to jobs module"

TARGET="$REPO_ROOT/hub/main.tf"

# Insert a new workload bucket extraction after the workload-b block in the
# jobs module. We match the closing paren of the last try() block.
sed -i.bak '/workload_b_security_logs_bucket_name = try(/,/^  )/{
/^  )/{
a\
  '"${ALIAS_UNDERSCORE}"'_security_logs_bucket_name = try(\
    [for w in var.workloads : w.storage.bucket_name if w.alias == "'"${ALIAS}"'"][0],\
    ""\
  )
}
}' "$TARGET"
rm -f "${TARGET}.bak"
modified_files+=("hub/main.tf")

# =============================================================================
# 7. Notebooks — Add widget + source path for each data source
# =============================================================================
# Uses parallel arrays for macOS bash 3 compatibility (no associative arrays).

NOTEBOOK_FILES=(
  "01_cloudtrail.py"
  "02_vpc_flow.py"
  "03_guardduty.py"
  "04_config.py"
)
NOTEBOOK_S3_PREFIXES=(
  "/cloudtrail/AWSLogs/"
  "/vpc-flow-logs/AWSLogs/"
  "/AWSLogs/"
  "/config/AWSLogs/"
)

# Derive the display label for notebook widgets (workload-c → Workload C)
DISPLAY_LABEL="$(echo "$ALIAS" | sed 's/-/ /g' | awk '{for(i=1;i<=NF;i++) $i=toupper(substr($i,1,1)) substr($i,2)}1')"

for i in "${!NOTEBOOK_FILES[@]}"; do
  notebook="${NOTEBOOK_FILES[$i]}"
  s3_prefix="${NOTEBOOK_S3_PREFIXES[$i]}"
  NOTEBOOK_FILE="$REPO_ROOT/notebooks/bronze/aws/$notebook"

  if [[ ! -f "$NOTEBOOK_FILE" ]]; then
    echo "  WARNING: $notebook not found, skipping"
    continue
  fi

  step "notebooks/bronze/aws/$notebook — adding ${ALIAS_UNDERSCORE} widget and source path"

  # 1. Add widget definition after the workload_b widget line
  sed -i.bak '/dbutils\.widgets\.text("workload_b_bucket"/a\
dbutils.widgets.text("'"${ALIAS_UNDERSCORE}"'_bucket", "", "'"${DISPLAY_LABEL}"' Bucket")' "$NOTEBOOK_FILE"
  rm -f "${NOTEBOOK_FILE}.bak"

  # 2. Add variable assignment after the workload_b assignment
  sed -i.bak '/^workload_b_bucket = dbutils\.widgets\.get("workload_b_bucket")/a\
'"${ALIAS_UNDERSCORE}"'_bucket = dbutils.widgets.get("'"${ALIAS_UNDERSCORE}"'_bucket")' "$NOTEBOOK_FILE"
  rm -f "${NOTEBOOK_FILE}.bak"

  # 3. Add entry in source_paths dict after the workload_b entry.
  #    Build the full line to insert, then use a temp file approach to avoid
  #    sed delimiter conflicts with s3://.
  NEW_PATH_LINE='    "'"${ALIAS_UNDERSCORE}"'": f"s3://{'"${ALIAS_UNDERSCORE}"'_bucket}'"${s3_prefix}"'",'
  {
    found=0
    while IFS= read -r line; do
      printf '%s\n' "$line"
      if [[ $found -eq 0 ]] && echo "$line" | grep -q '"workload_b".*workload_b_bucket'; then
        printf '%s\n' "$NEW_PATH_LINE"
        found=1
      fi
    done < "$NOTEBOOK_FILE"
  } > "${NOTEBOOK_FILE}.tmp"
  mv "${NOTEBOOK_FILE}.tmp" "$NOTEBOOK_FILE"

  modified_files+=("notebooks/bronze/aws/$notebook")
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
echo "New workload root: workloads/aws-${ALIAS}/"
echo "  (copied from workloads/_template-aws/ with terraform.tfvars and backend.tf)"
echo ""
echo "Next steps:"
echo "  1. Review changes:          git diff"
echo "  2. Init + apply workload:   cd workloads/aws-${ALIAS} && terraform init && terraform apply"
echo "  3. Assemble workloads:      cd ../.. && ./scripts/assemble-workloads.sh"
echo "  4. Apply hub:               cd hub && terraform apply"
echo "  5. Format Terraform:        terraform fmt -recursive ."
echo ""
echo "See onboarding_new_aws_accounts.md for detailed apply sequence and validation."
