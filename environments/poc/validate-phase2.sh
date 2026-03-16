#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Phase 2: Security Account Baseline Validation
# -----------------------------------------------------------------------------
# Verifies that Phase 2 resources (managed storage bucket + IAM roles) were
# created correctly and match the expected configuration.
# Run from any directory: ./environments/poc/validate-phase2.sh
# Exit code 0 = all checks passed, non-zero = failure.
# -----------------------------------------------------------------------------
set -uo pipefail

BUCKET="security-lakehouse-managed-<SECURITY_ACCOUNT_ID>"
MANAGED_STORAGE_ROLE="lakehouse-managed-storage-role"
HUB_ROLE="lakehouse-hub-role"
UC_MASTER_ROLE_ARN="arn:aws:iam::<DATABRICKS_AWS_ACCOUNT_ID>:role/unity-catalog-prod-UCMasterRole-<SUFFIX>"
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

echo "=== Phase 2: Security Account Baseline Validation ==="
echo ""

# ── S3 Managed Storage Bucket ───────────────────────────────────────────────

echo "--- Managed Storage Bucket ---"

# 1. Bucket exists
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "  PASS: Managed storage bucket exists"
  PASS=$((PASS + 1))
else
  echo "  FAIL: Managed storage bucket does not exist"
  FAIL=$((FAIL + 1))
fi

# 2. Bucket versioning
VERSIONING=$(aws s3api get-bucket-versioning --bucket "$BUCKET" --query 'Status' --output text 2>/dev/null)
check "Bucket versioning is Enabled" "$VERSIONING" "Enabled"

# 3. Bucket encryption
ENCRYPTION=$(aws s3api get-bucket-encryption --bucket "$BUCKET" \
  --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' \
  --output text 2>/dev/null)
check "Bucket encryption is AES256" "$ENCRYPTION" "AES256"

# 4. Public access block (all four flags)
PUB_BLOCK=$(aws s3api get-public-access-block --bucket "$BUCKET" \
  --query '[PublicAccessBlockConfiguration.BlockPublicAcls, PublicAccessBlockConfiguration.BlockPublicPolicy, PublicAccessBlockConfiguration.IgnorePublicAcls, PublicAccessBlockConfiguration.RestrictPublicBuckets]' \
  --output text 2>/dev/null)
check "Bucket public access fully blocked" "$PUB_BLOCK" "True	True	True	True"

# 5. Bucket is empty
OBJECT_COUNT=$(aws s3api list-objects-v2 --bucket "$BUCKET" --max-items 1 --query 'KeyCount' --output text 2>/dev/null)
check "Bucket is empty" "${OBJECT_COUNT:-0}" "0"

# 6. Bucket policy references UC master role
BUCKET_POLICY=$(aws s3api get-bucket-policy --bucket "$BUCKET" --query 'Policy' --output text 2>/dev/null)
if echo "$BUCKET_POLICY" | grep -q "$UC_MASTER_ROLE_ARN"; then
  echo "  PASS: Bucket policy includes UC master role"
  PASS=$((PASS + 1))
else
  echo "  FAIL: Bucket policy does not include UC master role"
  FAIL=$((FAIL + 1))
fi

echo ""

# ── Managed Storage IAM Role ────────────────────────────────────────────────

echo "--- Managed Storage IAM Role ---"

# 7. Role exists
MANAGED_ROLE_ARN=$(aws iam get-role --role-name "$MANAGED_STORAGE_ROLE" --query 'Role.Arn' --output text 2>/dev/null)
if [[ -n "$MANAGED_ROLE_ARN" && "$MANAGED_ROLE_ARN" != "None" ]]; then
  echo "  PASS: Managed storage role exists ($MANAGED_ROLE_ARN)"
  PASS=$((PASS + 1))
else
  echo "  FAIL: Managed storage role does not exist"
  FAIL=$((FAIL + 1))
fi

# 8. Trust policy includes UC master role
MANAGED_TRUST=$(aws iam get-role --role-name "$MANAGED_STORAGE_ROLE" --query 'Role.AssumeRolePolicyDocument' --output json 2>/dev/null)
if echo "$MANAGED_TRUST" | grep -q "$UC_MASTER_ROLE_ARN"; then
  echo "  PASS: Managed storage trust policy includes UC master role"
  PASS=$((PASS + 1))
else
  echo "  FAIL: Managed storage trust policy missing UC master role"
  FAIL=$((FAIL + 1))
fi

# 9. Trust policy requires external ID
if echo "$MANAGED_TRUST" | grep -q "sts:ExternalId"; then
  echo "  PASS: Managed storage trust policy requires external ID"
  PASS=$((PASS + 1))
else
  echo "  FAIL: Managed storage trust policy missing external ID condition"
  FAIL=$((FAIL + 1))
fi

# 10. Inline policy grants S3 access to managed bucket
MANAGED_POLICY=$(aws iam get-role-policy --role-name "$MANAGED_STORAGE_ROLE" --policy-name "managed-storage-s3-access" --query 'PolicyDocument' --output json 2>/dev/null)
if echo "$MANAGED_POLICY" | grep -q "s3:GetObject"; then
  echo "  PASS: Managed storage role has S3 access policy"
  PASS=$((PASS + 1))
else
  echo "  FAIL: Managed storage role missing S3 access policy"
  FAIL=$((FAIL + 1))
fi

echo ""

# ── Hub IAM Role ────────────────────────────────────────────────────────────

echo "--- Hub IAM Role ---"

# 11. Role exists
HUB_ROLE_ARN=$(aws iam get-role --role-name "$HUB_ROLE" --query 'Role.Arn' --output text 2>/dev/null)
if [[ -n "$HUB_ROLE_ARN" && "$HUB_ROLE_ARN" != "None" ]]; then
  echo "  PASS: Hub role exists ($HUB_ROLE_ARN)"
  PASS=$((PASS + 1))
else
  echo "  FAIL: Hub role does not exist"
  FAIL=$((FAIL + 1))
fi

# 12. Trust policy includes UC master role
HUB_TRUST=$(aws iam get-role --role-name "$HUB_ROLE" --query 'Role.AssumeRolePolicyDocument' --output json 2>/dev/null)
if echo "$HUB_TRUST" | grep -q "$UC_MASTER_ROLE_ARN"; then
  echo "  PASS: Hub trust policy includes UC master role"
  PASS=$((PASS + 1))
else
  echo "  FAIL: Hub trust policy missing UC master role"
  FAIL=$((FAIL + 1))
fi

# 13. Trust policy requires external ID
if echo "$HUB_TRUST" | grep -q "sts:ExternalId"; then
  echo "  PASS: Hub trust policy requires external ID"
  PASS=$((PASS + 1))
else
  echo "  FAIL: Hub trust policy missing external ID condition"
  FAIL=$((FAIL + 1))
fi

# 14. Inline policy allows AssumeRole to workload read-only roles
HUB_POLICY=$(aws iam get-role-policy --role-name "$HUB_ROLE" --policy-name "hub-role-chain-assume-and-s3" --query 'PolicyDocument' --output json 2>/dev/null)
if echo "$HUB_POLICY" | grep -q "sts:AssumeRole"; then
  echo "  PASS: Hub role can chain-assume into workload roles"
  PASS=$((PASS + 1))
else
  echo "  FAIL: Hub role missing chain-assume permission"
  FAIL=$((FAIL + 1))
fi

# 15. Inline policy allows S3 read on security log buckets
if echo "$HUB_POLICY" | grep -q "s3:GetObject"; then
  echo "  PASS: Hub role has S3 read access for security logs"
  PASS=$((PASS + 1))
else
  echo "  FAIL: Hub role missing S3 read access for security logs"
  FAIL=$((FAIL + 1))
fi

echo ""

# ── Terraform State Consistency ─────────────────────────────────────────────

echo "--- Terraform State ---"

# 16. Terraform state has expected resource count
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESOURCE_COUNT=$(terraform -chdir="$SCRIPT_DIR" state list 2>/dev/null | wc -l | tr -d ' ')
if [[ "$RESOURCE_COUNT" -ge 9 ]]; then
  echo "  PASS: Terraform state has ${RESOURCE_COUNT} resources (expected >= 9)"
  PASS=$((PASS + 1))
else
  echo "  FAIL: Terraform state has ${RESOURCE_COUNT} resources (expected >= 9)"
  FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="
exit "$FAIL"
