#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Phase 1 Bootstrap Validation
# -----------------------------------------------------------------------------
# Verifies that the bootstrap state backend resources were created correctly.
# Run from any directory: ./bootstrap/validate.sh
# Exit code 0 = all checks passed, non-zero = failure.
# -----------------------------------------------------------------------------
set -uo pipefail

BUCKET="security-lakehouse-tfstate-<SECURITY_ACCOUNT_ID>"
TABLE="security-lakehouse-tflock"
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
    echo "  FAIL: ${description} (got: ${result})"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== Phase 1: Bootstrap Validation ==="
echo ""

# 1. S3 bucket exists
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "  PASS: S3 state bucket exists"
  PASS=$((PASS + 1))
else
  echo "  FAIL: S3 state bucket does not exist"
  FAIL=$((FAIL + 1))
fi

# 2. Bucket versioning is enabled
VERSIONING=$(aws s3api get-bucket-versioning --bucket "$BUCKET" --query 'Status' --output text 2>/dev/null)
check "Bucket versioning is Enabled" "$VERSIONING" "Enabled"

# 3. Bucket encryption is AES256
ENCRYPTION=$(aws s3api get-bucket-encryption --bucket "$BUCKET" \
  --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' \
  --output text 2>/dev/null)
check "Bucket encryption is AES256" "$ENCRYPTION" "AES256"

# 4. Public access is fully blocked
PUB_BLOCK=$(aws s3api get-public-access-block --bucket "$BUCKET" \
  --query '[PublicAccessBlockConfiguration.BlockPublicAcls, PublicAccessBlockConfiguration.BlockPublicPolicy, PublicAccessBlockConfiguration.IgnorePublicAcls, PublicAccessBlockConfiguration.RestrictPublicBuckets]' \
  --output text 2>/dev/null)
check "Bucket public access is blocked (all four flags)" "$PUB_BLOCK" "True	True	True	True"

# 5. DynamoDB table exists and is ACTIVE
TABLE_STATUS=$(aws dynamodb describe-table --table-name "$TABLE" --query 'Table.TableStatus' --output text 2>/dev/null)
check "DynamoDB lock table is ACTIVE" "$TABLE_STATUS" "ACTIVE"

# 6. DynamoDB table has correct key schema
HASH_KEY=$(aws dynamodb describe-table --table-name "$TABLE" --query 'Table.KeySchema[0].AttributeName' --output text 2>/dev/null)
check "DynamoDB hash key is LockID" "$HASH_KEY" "LockID"

# 7. Backend init works
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
POC_DIR="${REPO_ROOT}/environments/poc"
if [ -f "${POC_DIR}/backend.tf" ]; then
  if terraform -chdir="$POC_DIR" init -backend=true -input=false -reconfigure > /dev/null 2>&1; then
    echo "  PASS: environments/poc terraform init succeeds"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: environments/poc terraform init failed"
    FAIL=$((FAIL + 1))
  fi
else
  echo "  SKIP: environments/poc/backend.tf not found"
fi

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="
exit "$FAIL"
