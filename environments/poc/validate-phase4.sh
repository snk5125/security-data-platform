#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Phase 4: Data Source Onboarding Validation
# -----------------------------------------------------------------------------
# Verifies that Phase 4 resources (S3 buckets, KMS keys, IAM roles, CloudTrail,
# VPC Flow Logs, GuardDuty, AWS Config) were created correctly in both workload
# accounts. Also validates the cross-account access chain:
#   security account credentials → hub role → workload read-only role → S3
#
# Run from any directory: ./environments/poc/validate-phase4.sh
# Exit code 0 = all checks passed, non-zero = failure.
# -----------------------------------------------------------------------------
set -uo pipefail

SECURITY_ACCOUNT_ID="<SECURITY_ACCOUNT_ID>"
WORKLOAD_A_ACCOUNT_ID="<WORKLOAD_A_ACCOUNT_ID>"
WORKLOAD_B_ACCOUNT_ID="<WORKLOAD_B_ACCOUNT_ID>"
WORKLOAD_A_ROLE="arn:aws:iam::${WORKLOAD_A_ACCOUNT_ID}:role/OrganizationAccountAccessRole"
WORKLOAD_B_ROLE="arn:aws:iam::${WORKLOAD_B_ACCOUNT_ID}:role/OrganizationAccountAccessRole"
HUB_ROLE_ARN="arn:aws:iam::${SECURITY_ACCOUNT_ID}:role/lakehouse-hub-role"
PASS=0
FAIL=0

check() {
  local description="$1"
  local result="$2"
  local expected="$3"

  if [[ "$result" == *"$expected"* ]]; then
    echo "  PASS: ${description}"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: ${description} (expected: ${expected}, got: ${result})"
    FAIL=$((FAIL + 1))
  fi
}

# Helper: assume role into an account and export temp credentials.
assume_role() {
  local role_arn="$1"
  local session_name="$2"
  local creds

  creds=$(aws sts assume-role --role-arn "$role_arn" --role-session-name "$session_name" --output json 2>/dev/null)
  if [[ -z "$creds" ]]; then
    echo "  FAIL: Could not assume role ${role_arn}"
    FAIL=$((FAIL + 1))
    return 1
  fi

  export AWS_ACCESS_KEY_ID=$(echo "$creds" | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['AccessKeyId'])")
  export AWS_SECRET_ACCESS_KEY=$(echo "$creds" | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['SecretAccessKey'])")
  export AWS_SESSION_TOKEN=$(echo "$creds" | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['SessionToken'])")
  return 0
}

# Helper: clear assumed role credentials, revert to caller identity.
clear_role() {
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
}

# Helper: validate one workload account's data source resources.
validate_account() {
  local alias="$1"
  local role_arn="$2"
  local account_id="$3"
  local name_prefix="lakehouse-${alias}"
  local bucket_name="${name_prefix}-security-logs-${account_id}"

  echo "--- ${alias} (via assume-role) ---"

  if ! assume_role "$role_arn" "phase4-validate-${alias}"; then
    return
  fi

  # 1. S3 bucket exists
  local bucket_exists
  bucket_exists=$(aws s3api head-bucket --bucket "$bucket_name" 2>&1)
  if [[ $? -eq 0 ]]; then
    echo "  PASS: Security-logs bucket exists (${bucket_name})"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: Security-logs bucket not found (${bucket_name})"
    FAIL=$((FAIL + 1))
    clear_role
    return
  fi

  # 2. Bucket versioning enabled
  local versioning
  versioning=$(aws s3api get-bucket-versioning --bucket "$bucket_name" \
    --query 'Status' --output text 2>/dev/null)
  check "Bucket versioning enabled" "$versioning" "Enabled"

  # 3. Bucket encryption (AES256)
  local encryption
  encryption=$(aws s3api get-bucket-encryption --bucket "$bucket_name" \
    --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' \
    --output text 2>/dev/null)
  check "Bucket encryption is AES256" "$encryption" "AES256"

  # 4. Bucket public access block (all four flags)
  local public_block
  public_block=$(aws s3api get-public-access-block --bucket "$bucket_name" \
    --query '[PublicAccessBlockConfiguration.BlockPublicAcls, PublicAccessBlockConfiguration.BlockPublicPolicy, PublicAccessBlockConfiguration.IgnorePublicAcls, PublicAccessBlockConfiguration.RestrictPublicBuckets]' \
    --output text 2>/dev/null)
  check "Bucket public access fully blocked" "$public_block" "True	True	True	True"

  # 5. Bucket policy exists and has CloudTrail statement
  local bucket_policy
  bucket_policy=$(aws s3api get-bucket-policy --bucket "$bucket_name" \
    --query 'Policy' --output text 2>/dev/null)
  check "Bucket policy grants CloudTrail access" "$bucket_policy" "CloudTrailAclCheck"
  check "Bucket policy grants GuardDuty access" "$bucket_policy" "GuardDutyWrite"
  check "Bucket policy grants Config access" "$bucket_policy" "ConfigWrite"

  # 6. KMS key exists with GuardDuty alias
  local kms_alias
  kms_alias=$(aws kms list-aliases \
    --query "Aliases[?AliasName=='alias/${name_prefix}-guardduty'].AliasName" \
    --output text 2>/dev/null)
  check "KMS key alias exists" "$kms_alias" "alias/${name_prefix}-guardduty"

  # 7. Read-only IAM role exists
  local read_only_role
  read_only_role=$(aws iam get-role --role-name "${name_prefix}-read-only-role" \
    --query 'Role.RoleName' --output text 2>/dev/null)
  check "Read-only IAM role exists" "$read_only_role" "${name_prefix}-read-only-role"

  # 8. Read-only role trusts the hub role
  local trust_policy
  trust_policy=$(aws iam get-role --role-name "${name_prefix}-read-only-role" \
    --query 'Role.AssumeRolePolicyDocument' --output text 2>/dev/null)
  check "Read-only role trusts hub role" "$trust_policy" "$HUB_ROLE_ARN"

  # 9. CloudTrail trail exists and is logging
  local trail_status
  trail_status=$(aws cloudtrail get-trail-status --name "${name_prefix}-trail" \
    --query 'IsLogging' --output text 2>/dev/null)
  check "CloudTrail trail is logging" "$trail_status" "True"

  # 10. VPC Flow Log exists
  local flow_log_id
  flow_log_id=$(aws ec2 describe-flow-logs \
    --filter "Name=tag:Name,Values=${name_prefix}-vpc-flow-logs" \
    --query 'FlowLogs[0].FlowLogId' --output text 2>/dev/null)
  if [[ -n "$flow_log_id" && "$flow_log_id" != "None" ]]; then
    echo "  PASS: VPC Flow Log exists (${flow_log_id})"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: VPC Flow Log not found"
    FAIL=$((FAIL + 1))
  fi

  # 11. GuardDuty detector is enabled
  local detector_id
  detector_id=$(aws guardduty list-detectors --query 'DetectorIds[0]' --output text 2>/dev/null)
  if [[ -n "$detector_id" && "$detector_id" != "None" ]]; then
    local detector_status
    detector_status=$(aws guardduty get-detector --detector-id "$detector_id" \
      --query 'Status' --output text 2>/dev/null)
    check "GuardDuty detector is enabled" "$detector_status" "ENABLED"

    # 12. GuardDuty has S3 publishing destination
    local pub_dest
    pub_dest=$(aws guardduty list-publishing-destinations --detector-id "$detector_id" \
      --query 'Destinations[0].DestinationType' --output text 2>/dev/null)
    check "GuardDuty S3 export configured" "$pub_dest" "S3"
  else
    echo "  FAIL: GuardDuty detector not found"
    FAIL=$((FAIL + 2))
  fi

  # 13. Config recorder is recording
  local recorder_status
  recorder_status=$(aws configservice describe-configuration-recorder-status \
    --query 'ConfigurationRecordersStatus[0].recording' --output text 2>/dev/null)
  check "Config recorder is recording" "$recorder_status" "True"

  # 14. Config delivery channel exists
  local delivery_channel
  delivery_channel=$(aws configservice describe-delivery-channels \
    --query 'DeliveryChannels[0].s3BucketName' --output text 2>/dev/null)
  check "Config delivery channel targets correct bucket" "$delivery_channel" "$bucket_name"

  # 15. Config IAM role exists
  local config_role
  config_role=$(aws iam get-role --role-name "${name_prefix}-config-role" \
    --query 'Role.RoleName' --output text 2>/dev/null)
  check "Config IAM role exists" "$config_role" "${name_prefix}-config-role"

  clear_role
  echo ""
}

echo "=== Phase 4: Data Source Onboarding Validation ==="
echo ""

# ── Validate each workload account ──────────────────────────────────────────

validate_account "workload-a" "$WORKLOAD_A_ROLE" "$WORKLOAD_A_ACCOUNT_ID"
validate_account "workload-b" "$WORKLOAD_B_ROLE" "$WORKLOAD_B_ACCOUNT_ID"

# ── Cross-Account Access Chain ─────────────────────────────────────────────
# Test the full chain: caller → hub role → workload read-only role → S3 ls

echo "--- Cross-Account Access Chain ---"

# Step 1: Assume the hub role in the security account
if assume_role "$HUB_ROLE_ARN" "phase4-validate-chain"; then
  echo "  PASS: Assumed hub role"

  # Step 2: From hub role, assume read-only role in workload-a
  local_a_role="arn:aws:iam::${WORKLOAD_A_ACCOUNT_ID}:role/lakehouse-workload-a-read-only-role"
  chain_creds=$(aws sts assume-role --role-arn "$local_a_role" --role-session-name "chain-test" --output json 2>/dev/null)
  if [[ -n "$chain_creds" ]]; then
    echo "  PASS: Hub role → workload-a read-only role chain works"
    PASS=$((PASS + 1))

    # Step 3: Use chained creds to list bucket
    export AWS_ACCESS_KEY_ID=$(echo "$chain_creds" | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['AccessKeyId'])")
    export AWS_SECRET_ACCESS_KEY=$(echo "$chain_creds" | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['SecretAccessKey'])")
    export AWS_SESSION_TOKEN=$(echo "$chain_creds" | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['SessionToken'])")

    bucket_ls=$(aws s3 ls "s3://lakehouse-workload-a-security-logs-${WORKLOAD_A_ACCOUNT_ID}/" 2>/dev/null)
    if [[ $? -eq 0 ]]; then
      echo "  PASS: Chained role can list security-logs bucket"
      PASS=$((PASS + 1))
    else
      echo "  FAIL: Chained role cannot list security-logs bucket"
      FAIL=$((FAIL + 1))
    fi
  else
    echo "  FAIL: Hub role cannot assume workload-a read-only role"
    FAIL=$((FAIL + 2))
  fi
  clear_role
else
  echo "  FAIL: Could not assume hub role — skipping chain test"
  FAIL=$((FAIL + 3))
fi

# ── Terraform State Consistency ─────────────────────────────────────────────

echo ""
echo "--- Terraform State ---"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESOURCE_COUNT=$(terraform -chdir="$SCRIPT_DIR" state list 2>/dev/null | wc -l | tr -d ' ')
# Phase 2 = 9, Phase 3 = 20, Phase 4 = 36, data sources ~18 = ~83 total
if [[ "$RESOURCE_COUNT" -ge 65 ]]; then
  echo "  PASS: Terraform state has ${RESOURCE_COUNT} resources (expected >= 65)"
  PASS=$((PASS + 1))
else
  echo "  FAIL: Terraform state has ${RESOURCE_COUNT} resources (expected >= 65)"
  FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="
exit "$FAIL"
