#!/usr/bin/env bash
# =============================================================================
# onboard_workload_account.sh — Onboard a new AWS workload account
# =============================================================================
# Modifies all necessary Terraform and notebook files to add a new workload
# account to the security lakehouse. Does NOT run terraform apply.
#
# Usage:
#   ./onboard_workload_account.sh \
#     --alias workload-c \
#     --account-id 123456789012 \
#     --vpc-cidr 10.2.0.0/16 \
#     --subnet-cidr 10.2.1.0/24
#
# Prerequisites:
#   - The account must be an AWS Organizations member with
#     OrganizationAccountAccessRole available.
#   - Choose a VPC CIDR that does not overlap with existing workloads:
#       workload-a = 10.0.0.0/16, workload-b = 10.1.0.0/16
#
# What this script modifies (16 files):
#   environments/poc/variables.tf        — new account ID variable
#   environments/poc/terraform.tfvars    — new account ID value
#   environments/poc/providers.tf        — new AWS provider alias
#   environments/poc/main.tf             — new baseline + data sources modules,
#                                          updated cloud_integration + bronze_ingestion
#   environments/poc/outputs.tf          — new output blocks
#   modules/databricks/cloud-integration/variables.tf  — new bucket variable
#   modules/databricks/cloud-integration/main.tf       — new external location
#   modules/databricks/cloud-integration/outputs.tf    — new output
#   modules/databricks/jobs/variables.tf               — new bucket variable
#   modules/databricks/jobs/main.tf                    — updated common_params
#   notebooks/bronze/*.py (4 files)                    — new widget + source path
#
# After running this script:
#   cd environments/poc
#   terraform fmt -recursive ../..
#   terraform validate
#   terraform plan
#   terraform apply    # or staged apply per onboarding guide
# =============================================================================

set -euo pipefail

# ── Repo root detection ──────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

# Verify we're in the right repo
if [[ ! -d "$REPO_ROOT/environments/poc" ]] || [[ ! -d "$REPO_ROOT/modules/databricks" ]]; then
  echo "ERROR: Script must be run from the repository root (databricks-security-lakehouse/)."
  echo "       Expected directories: environments/poc/, modules/databricks/"
  exit 1
fi

# ── Argument parsing ─────────────────────────────────────────────────────────
ALIAS=""
ACCOUNT_ID=""
VPC_CIDR=""
SUBNET_CIDR=""

usage() {
  cat <<'USAGE'
Usage: onboard_workload_account.sh --alias <name> --account-id <12-digit> --vpc-cidr <cidr> --subnet-cidr <cidr>

Required arguments:
  --alias       Workload account alias (e.g., workload-c). Used in all resource
                naming. Must be lowercase, alphanumeric + hyphens.
  --account-id  12-digit AWS account ID for the new workload account.
  --vpc-cidr    VPC CIDR block (e.g., 10.2.0.0/16). Must not overlap existing.
  --subnet-cidr Public subnet CIDR (e.g., 10.2.1.0/24). Must be within vpc-cidr.

Example:
  ./onboard_workload_account.sh \
    --alias workload-c \
    --account-id 123456789012 \
    --vpc-cidr 10.2.0.0/16 \
    --subnet-cidr 10.2.1.0/24
USAGE
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --alias)       ALIAS="$2"; shift 2 ;;
    --account-id)  ACCOUNT_ID="$2"; shift 2 ;;
    --vpc-cidr)    VPC_CIDR="$2"; shift 2 ;;
    --subnet-cidr) SUBNET_CIDR="$2"; shift 2 ;;
    -h|--help)     usage ;;
    *)             echo "ERROR: Unknown argument: $1"; usage ;;
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

# ── Idempotency check ───────────────────────────────────────────────────────
if grep -q "variable \"${ALIAS_UNDERSCORE}_account_id\"" "$REPO_ROOT/environments/poc/variables.tf" 2>/dev/null; then
  echo "ERROR: Account alias '${ALIAS}' appears to be already onboarded."
  echo "       Found '${ALIAS_UNDERSCORE}_account_id' in environments/poc/variables.tf"
  exit 1
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo "============================================================"
echo "  Onboarding new workload account"
echo "============================================================"
echo "  Alias:       $ALIAS"
echo "  Account ID:  $ACCOUNT_ID"
echo "  VPC CIDR:    $VPC_CIDR"
echo "  Subnet CIDR: $SUBNET_CIDR"
echo "  TF variable: ${ALIAS_UNDERSCORE}_account_id"
echo "============================================================"
echo ""

STEP=0
modified_files=()

step() {
  STEP=$((STEP + 1))
  echo "[$STEP] $1"
}

# =============================================================================
# 1. environments/poc/variables.tf — Add account ID variable
# =============================================================================
step "environments/poc/variables.tf — adding ${ALIAS_UNDERSCORE}_account_id variable"

TARGET="$REPO_ROOT/environments/poc/variables.tf"

# Insert before organization_id, which follows all workload account IDs.
sed -i.bak '/^variable "organization_id"/i\
variable "'"${ALIAS_UNDERSCORE}"'_account_id" {\
  description = "AWS account ID of '"${ALIAS}"' (hosts VPC, EC2, security data sources)"\
  type        = string\
}\
' "$TARGET"
rm -f "${TARGET}.bak"
modified_files+=("$TARGET")

# =============================================================================
# 2. environments/poc/terraform.tfvars — Add account ID value
# =============================================================================
step "environments/poc/terraform.tfvars — adding ${ALIAS_UNDERSCORE}_account_id value"

TARGET="$REPO_ROOT/environments/poc/terraform.tfvars"

# Insert before organization_id line.
sed -i.bak '/^organization_id/i\
'"${ALIAS_UNDERSCORE}"'_account_id = "'"${ACCOUNT_ID}"'"' "$TARGET"
rm -f "${TARGET}.bak"
modified_files+=("$TARGET")

# =============================================================================
# 3. environments/poc/providers.tf — Add AWS provider alias
# =============================================================================
step "environments/poc/providers.tf — adding aws.${ALIAS_UNDERSCORE} provider"

TARGET="$REPO_ROOT/environments/poc/providers.tf"

# Insert before the Databricks provider block.
sed -i.bak '/^# ── Databricks workspace provider/i\
# ── '"${ALIAS}"' ────────────────────────────────────────────────────────\
# Same pattern as existing workloads — assumes into the member account.\
provider "aws" {\
  alias  = "'"${ALIAS_UNDERSCORE}"'"\
  region = var.aws_region\
\
  assume_role {\
    role_arn = "arn:aws:iam::${var.'"${ALIAS_UNDERSCORE}"'_account_id}:role/OrganizationAccountAccessRole"\
  }\
\
  default_tags {\
    tags = local.common_tags\
  }\
}\
' "$TARGET"
rm -f "${TARGET}.bak"
modified_files+=("$TARGET")

# =============================================================================
# 4. environments/poc/main.tf — Add baseline module (Phase 3)
# =============================================================================
step "environments/poc/main.tf — adding ${ALIAS_UNDERSCORE}_baseline module"

TARGET="$REPO_ROOT/environments/poc/main.tf"

# Anchor on "# Phase 4:" which is on its own line below the ═ separator.
sed -i.bak '/^# Phase 4: Data Source Onboarding/i\
module "'"${ALIAS_UNDERSCORE}"'_baseline" {\
  source = "../../modules/aws/workload-account-baseline"\
\
  providers = {\
    aws = aws.'"${ALIAS_UNDERSCORE}"'\
  }\
\
  account_alias      = "'"${ALIAS}"'"\
  account_id         = var.'"${ALIAS_UNDERSCORE}"'_account_id\
  vpc_cidr           = "'"${VPC_CIDR}"'"\
  public_subnet_cidr = "'"${SUBNET_CIDR}"'"\
\
  tags = local.common_tags\
}\
' "$TARGET"
rm -f "${TARGET}.bak"
modified_files+=("$TARGET")

# =============================================================================
# 5. environments/poc/main.tf — Add data sources module (Phase 4)
# =============================================================================
step "environments/poc/main.tf — adding ${ALIAS_UNDERSCORE}_data_sources module"

# Anchor on "# Phase 5:" which is on its own line below the ═ separator.
sed -i.bak '/^# Phase 5: Databricks Cloud Integration/i\
module "'"${ALIAS_UNDERSCORE}"'_data_sources" {\
  source = "../../modules/aws/data-sources"\
\
  providers = {\
    aws = aws.'"${ALIAS_UNDERSCORE}"'\
  }\
\
  account_alias = "'"${ALIAS}"'"\
  account_id    = var.'"${ALIAS_UNDERSCORE}"'_account_id\
  region        = var.aws_region\
  vpc_id        = module.'"${ALIAS_UNDERSCORE}"'_baseline.vpc_id\
  hub_role_arn  = module.security_account_baseline.hub_role_arn\
\
  tags = local.common_tags\
}\
' "$TARGET"
rm -f "${TARGET}.bak"
modified_files+=("$TARGET")

# =============================================================================
# 6. environments/poc/main.tf — Update cloud_integration module (Phase 5)
# =============================================================================
step "environments/poc/main.tf — updating cloud_integration module"

# Add the new bucket variable line after workload_b's line in cloud_integration.
# The line ends with workload_b_data_sources.security_logs_bucket_name and is
# followed by a closing brace (}). We use sed to append the new line.
sed -i.bak '/workload_b_security_logs_bucket_name = module\.workload_b_data_sources\.security_logs_bucket_name/a\
  '"${ALIAS_UNDERSCORE}"'_security_logs_bucket_name = module.'"${ALIAS_UNDERSCORE}"'_data_sources.security_logs_bucket_name' "$TARGET"
rm -f "${TARGET}.bak"
modified_files+=("$TARGET")

# =============================================================================
# 7. environments/poc/main.tf — Update bronze_ingestion module (Phase 8)
# =============================================================================
step "environments/poc/main.tf — updating bronze_ingestion module"

# The cloud_integration block was already modified in step 6, so the first
# occurrence now has the new alias on the next line. The bronze_ingestion block
# still has workload_b as the last line. We match lines NOT followed by our alias.
sed -i.bak '/^  workload_b_security_logs_bucket_name = module\.workload_b_data_sources\.security_logs_bucket_name$/{
N
/'"${ALIAS_UNDERSCORE}"'_security_logs_bucket_name/!{
s|\(workload_b_data_sources\.security_logs_bucket_name\)\n|\1\
  '"${ALIAS_UNDERSCORE}"'_security_logs_bucket_name = module.'"${ALIAS_UNDERSCORE}"'_data_sources.security_logs_bucket_name\
|
}
}' "$TARGET"
rm -f "${TARGET}.bak"
modified_files+=("$TARGET")

# =============================================================================
# 8. environments/poc/outputs.tf — Add output blocks for new workload
# =============================================================================
step "environments/poc/outputs.tf — adding output blocks"

TARGET="$REPO_ROOT/environments/poc/outputs.tf"

# Insert before the Phase 5 outputs section.
sed -i.bak '/^# ── Phase 5: Databricks Cloud Integration/i\
# ── '"${ALIAS}"' outputs ──────────────────────────────────────────────────────\
\
output "'"${ALIAS_UNDERSCORE}"'_vpc_id" {\
  description = "'"${ALIAS}"' VPC ID"\
  value       = module.'"${ALIAS_UNDERSCORE}"'_baseline.vpc_id\
}\
\
output "'"${ALIAS_UNDERSCORE}"'_linux_instance_id" {\
  description = "'"${ALIAS}"' Linux EC2 instance ID"\
  value       = module.'"${ALIAS_UNDERSCORE}"'_baseline.linux_instance_id\
}\
\
output "'"${ALIAS_UNDERSCORE}"'_windows_instance_id" {\
  description = "'"${ALIAS}"' Windows EC2 instance ID"\
  value       = module.'"${ALIAS_UNDERSCORE}"'_baseline.windows_instance_id\
}\
\
output "'"${ALIAS_UNDERSCORE}"'_security_logs_bucket_arn" {\
  description = "'"${ALIAS}"' security-logs bucket ARN"\
  value       = module.'"${ALIAS_UNDERSCORE}"'_data_sources.security_logs_bucket_arn\
}\
\
output "'"${ALIAS_UNDERSCORE}"'_security_logs_bucket_name" {\
  description = "'"${ALIAS}"' security-logs bucket name"\
  value       = module.'"${ALIAS_UNDERSCORE}"'_data_sources.security_logs_bucket_name\
}\
\
output "'"${ALIAS_UNDERSCORE}"'_read_only_role_arn" {\
  description = "'"${ALIAS}"' read-only IAM role ARN"\
  value       = module.'"${ALIAS_UNDERSCORE}"'_data_sources.read_only_role_arn\
}\
' "$TARGET"
rm -f "${TARGET}.bak"
modified_files+=("$TARGET")

# =============================================================================
# 9. modules/databricks/cloud-integration/variables.tf — Add bucket variable
# =============================================================================
step "modules/databricks/cloud-integration/variables.tf — adding bucket variable"

TARGET="$REPO_ROOT/modules/databricks/cloud-integration/variables.tf"

cat >> "$TARGET" <<EOF

variable "${ALIAS_UNDERSCORE}_security_logs_bucket_name" {
  description = "Name of the security-logs S3 bucket in ${ALIAS} (e.g., 'lakehouse-${ALIAS}-security-logs-123456')."
  type        = string
}
EOF
modified_files+=("$TARGET")

# =============================================================================
# 10. modules/databricks/cloud-integration/main.tf — Add external location
# =============================================================================
step "modules/databricks/cloud-integration/main.tf — adding external location"

TARGET="$REPO_ROOT/modules/databricks/cloud-integration/main.tf"

# Insert before "# Managed storage" comment.
sed -i.bak '/^# Managed storage —/i\
# '"${ALIAS}"' security logs — same data sources as existing workloads\
# from the new workload account.\
resource "databricks_external_location" "'"${ALIAS_UNDERSCORE}"'" {\
  name            = "'"${ALIAS}"'-security-logs"\
  url             = "s3://${var.'"${ALIAS_UNDERSCORE}"'_security_logs_bucket_name}/"\
  credential_name = databricks_storage_credential.hub.name\
  comment         = "Security logs from '"${ALIAS}"' (CloudTrail, Flow Logs, GuardDuty, Config)"\
\
  read_only = true\
\
  depends_on = [databricks_storage_credential.hub]\
}\
' "$TARGET"
rm -f "${TARGET}.bak"
modified_files+=("$TARGET")

# =============================================================================
# 11. modules/databricks/cloud-integration/outputs.tf — Add output
# =============================================================================
step "modules/databricks/cloud-integration/outputs.tf — adding external location output"

TARGET="$REPO_ROOT/modules/databricks/cloud-integration/outputs.tf"

sed -i.bak '/^output "managed_external_location_url"/i\
output "'"${ALIAS_UNDERSCORE}"'_external_location_url" {\
  description = "S3 URL for '"${ALIAS}"' external location"\
  value       = databricks_external_location.'"${ALIAS_UNDERSCORE}"'.url\
}\
' "$TARGET"
rm -f "${TARGET}.bak"
modified_files+=("$TARGET")

# =============================================================================
# 12. modules/databricks/jobs/variables.tf — Add bucket variable
# =============================================================================
step "modules/databricks/jobs/variables.tf — adding bucket variable"

TARGET="$REPO_ROOT/modules/databricks/jobs/variables.tf"

sed -i.bak '/^variable "notebook_source_dir"/i\
variable "'"${ALIAS_UNDERSCORE}"'_security_logs_bucket_name" {\
  description = "'"${ALIAS}"' security logs S3 bucket name — source for Auto Loader"\
  type        = string\
}\
' "$TARGET"
rm -f "${TARGET}.bak"
modified_files+=("$TARGET")

# =============================================================================
# 13. modules/databricks/jobs/main.tf — Update common_params
# =============================================================================
step "modules/databricks/jobs/main.tf — updating common_params"

TARGET="$REPO_ROOT/modules/databricks/jobs/main.tf"

sed -i.bak '/workload_b_bucket = var\.workload_b_security_logs_bucket_name/a\
    '"${ALIAS_UNDERSCORE}"'_bucket = var.'"${ALIAS_UNDERSCORE}"'_security_logs_bucket_name' "$TARGET"
rm -f "${TARGET}.bak"
modified_files+=("$TARGET")

# =============================================================================
# 14–17. Notebooks — Add widget + source path for each data source
# =============================================================================
# Uses parallel arrays for macOS bash 3 compatibility (no associative arrays).

NOTEBOOK_FILES=(
  "01_bronze_cloudtrail.py"
  "02_bronze_vpc_flow.py"
  "03_bronze_guardduty.py"
  "04_bronze_config.py"
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
  NOTEBOOK_FILE="$REPO_ROOT/notebooks/bronze/$notebook"

  if [[ ! -f "$NOTEBOOK_FILE" ]]; then
    echo "  WARNING: $notebook not found, skipping"
    continue
  fi

  step "notebooks/bronze/$notebook — adding ${ALIAS_UNDERSCORE} widget and source path"

  # Escape the S3 prefix for sed replacement (forward slashes)
  s3_prefix_escaped="$(echo "$s3_prefix" | sed 's/\//\\\//g')"

  # 1. Add widget definition after the workload_b widget line
  sed -i.bak '/dbutils\.widgets\.text("workload_b_bucket"/a\
dbutils.widgets.text("'"${ALIAS_UNDERSCORE}"'_bucket", "", "'"${DISPLAY_LABEL}"' Bucket")' "$NOTEBOOK_FILE"
  rm -f "${NOTEBOOK_FILE}.bak"

  # 2. Add variable assignment after the workload_b assignment
  sed -i.bak '/^workload_b_bucket = dbutils\.widgets\.get("workload_b_bucket")/a\
'"${ALIAS_UNDERSCORE}"'_bucket = dbutils.widgets.get("'"${ALIAS_UNDERSCORE}"'_bucket")' "$NOTEBOOK_FILE"
  rm -f "${NOTEBOOK_FILE}.bak"

  # 3. Add entry in source_paths dict after the workload_b entry.
  #    Build the full line to insert, then use sed to place it.
  NEW_PATH_LINE='    "'"${ALIAS_UNDERSCORE}"'": f"s3://{'"${ALIAS_UNDERSCORE}"'_bucket}'"${s3_prefix}"'",'
  # Use a temp file approach to avoid sed delimiter conflicts with s3://
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

  modified_files+=("$NOTEBOOK_FILE")
done

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "============================================================"
echo "  Onboarding complete — ${#modified_files[@]} files modified"
echo "============================================================"
echo ""
echo "Modified files:"
for f in "${modified_files[@]}"; do
  echo "  ${f#$REPO_ROOT/}"
done
echo ""
echo "Next steps:"
echo "  1. Review changes:     git diff"
echo "  2. Format Terraform:   cd environments/poc && terraform fmt -recursive ../.."
echo "  3. Validate:           terraform validate"
echo "  4. Plan:               terraform plan"
echo "  5. Apply (staged):     terraform apply -target=module.${ALIAS_UNDERSCORE}_baseline"
echo "                         terraform apply -target=module.${ALIAS_UNDERSCORE}_data_sources"
echo "                         terraform apply"
echo ""
echo "See onboarding_new_aws_accounts.md for detailed apply sequence and validation."
