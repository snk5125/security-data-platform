#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Phase 6: Unity Catalog Validation
# -----------------------------------------------------------------------------
# Verifies that Phase 6 resources (catalog, schemas, grants) were created
# correctly in the Databricks workspace.
#
# Prerequisites:
#   - DATABRICKS_HOST and DATABRICKS_TOKEN environment variables set, OR
#   - Pass workspace URL and PAT as arguments:
#       ./validate-phase6.sh https://dbc-xxx.cloud.databricks.com dapi...
#
# Exit code 0 = all checks passed, non-zero = failure.
# -----------------------------------------------------------------------------
set -uo pipefail

# Accept workspace URL and PAT from args or environment
DATABRICKS_HOST="${1:-${DATABRICKS_HOST:-}}"
DATABRICKS_TOKEN="${2:-${DATABRICKS_TOKEN:-}}"

if [[ -z "$DATABRICKS_HOST" || -z "$DATABRICKS_TOKEN" ]]; then
  echo "Usage: $0 <workspace-url> <pat>"
  echo "  or set DATABRICKS_HOST and DATABRICKS_TOKEN environment variables"
  exit 1
fi

# Strip trailing slash from host
DATABRICKS_HOST="${DATABRICKS_HOST%/}"

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

# Helper: call Databricks REST API
db_api() {
  local method="$1"
  local endpoint="$2"
  local data="${3:-}"

  if [[ -n "$data" ]]; then
    curl -s -X "$method" \
      -H "Authorization: Bearer ${DATABRICKS_TOKEN}" \
      -H "Content-Type: application/json" \
      "${DATABRICKS_HOST}/api/2.1${endpoint}" \
      -d "$data"
  else
    curl -s -X "$method" \
      -H "Authorization: Bearer ${DATABRICKS_TOKEN}" \
      "${DATABRICKS_HOST}/api/2.1${endpoint}"
  fi
}

echo "=== Phase 6: Unity Catalog Validation ==="
echo ""

# ── Catalog ──────────────────────────────────────────────────────────────────

echo "--- Catalog ---"

# 1. Catalog exists
catalog=$(db_api GET "/unity-catalog/catalogs/security_poc")
catalog_name=$(echo "$catalog" | python3 -c "import sys,json; print(json.load(sys.stdin).get('name',''))" 2>/dev/null)
check "Catalog 'security_poc' exists" "$catalog_name" "security_poc"

# 2. Catalog has correct comment
catalog_comment=$(echo "$catalog" | python3 -c "import sys,json; print(json.load(sys.stdin).get('comment',''))" 2>/dev/null)
check "Catalog has correct comment" "$catalog_comment" "Security data lakehouse PoC"

# 3. Catalog has storage root under managed bucket
catalog_storage=$(echo "$catalog" | python3 -c "import sys,json; print(json.load(sys.stdin).get('storage_location',''))" 2>/dev/null)
check "Catalog storage root under managed bucket" "$catalog_storage" "security-lakehouse-managed"

echo ""

# ── Schemas ──────────────────────────────────────────────────────────────────

echo "--- Schemas ---"

# 4. Bronze schema exists
bronze=$(db_api GET "/unity-catalog/schemas/security_poc.bronze")
bronze_name=$(echo "$bronze" | python3 -c "import sys,json; print(json.load(sys.stdin).get('name',''))" 2>/dev/null)
check "Bronze schema exists" "$bronze_name" "bronze"

# 5. Bronze schema has correct comment
bronze_comment=$(echo "$bronze" | python3 -c "import sys,json; print(json.load(sys.stdin).get('comment',''))" 2>/dev/null)
check "Bronze schema has correct comment" "$bronze_comment" "Raw, immutable ingest layer"

# 6. Silver schema exists
silver=$(db_api GET "/unity-catalog/schemas/security_poc.silver")
silver_name=$(echo "$silver" | python3 -c "import sys,json; print(json.load(sys.stdin).get('name',''))" 2>/dev/null)
check "Silver schema exists" "$silver_name" "silver"

# 7. Silver schema has correct comment
silver_comment=$(echo "$silver" | python3 -c "import sys,json; print(json.load(sys.stdin).get('comment',''))" 2>/dev/null)
check "Silver schema has correct comment" "$silver_comment" "Normalized, typed"

# 8. Gold schema exists
gold=$(db_api GET "/unity-catalog/schemas/security_poc.gold")
gold_name=$(echo "$gold" | python3 -c "import sys,json; print(json.load(sys.stdin).get('name',''))" 2>/dev/null)
check "Gold schema exists" "$gold_name" "gold"

# 9. Gold schema has correct comment
gold_comment=$(echo "$gold" | python3 -c "import sys,json; print(json.load(sys.stdin).get('comment',''))" 2>/dev/null)
check "Gold schema has correct comment" "$gold_comment" "Analytical products"

echo ""

# ── Grants ───────────────────────────────────────────────────────────────────

echo "--- Grants ---"

# 10. Catalog grants include USE_CATALOG for account users
cat_grants=$(db_api GET "/unity-catalog/permissions/catalog/security_poc")
cat_use=$(echo "$cat_grants" | python3 -c "
import sys,json
data = json.load(sys.stdin)
for a in data.get('privilege_assignments',[]):
    if a.get('principal','') == 'account users':
        for p in a.get('privileges',[]):
            if p == 'USE_CATALOG':
                print('USE_CATALOG')
                break
" 2>/dev/null)
check "Catalog grants: USE_CATALOG for account users" "$cat_use" "USE_CATALOG"

# 11. Catalog grants include CREATE_SCHEMA for account users
cat_create=$(echo "$cat_grants" | python3 -c "
import sys,json
data = json.load(sys.stdin)
for a in data.get('privilege_assignments',[]):
    if a.get('principal','') == 'account users':
        for p in a.get('privileges',[]):
            if p == 'CREATE_SCHEMA':
                print('CREATE_SCHEMA')
                break
" 2>/dev/null)
check "Catalog grants: CREATE_SCHEMA for account users" "$cat_create" "CREATE_SCHEMA"

# 12. Bronze schema grants include USE_SCHEMA
bronze_grants=$(db_api GET "/unity-catalog/permissions/schema/security_poc.bronze")
bronze_use=$(echo "$bronze_grants" | python3 -c "
import sys,json
data = json.load(sys.stdin)
for a in data.get('privilege_assignments',[]):
    if a.get('principal','') == 'account users':
        for p in a.get('privileges',[]):
            if p == 'USE_SCHEMA':
                print('USE_SCHEMA')
                break
" 2>/dev/null)
check "Bronze schema grants: USE_SCHEMA for account users" "$bronze_use" "USE_SCHEMA"

# 13. Bronze schema grants include CREATE_TABLE
bronze_ct=$(echo "$bronze_grants" | python3 -c "
import sys,json
data = json.load(sys.stdin)
for a in data.get('privilege_assignments',[]):
    if a.get('principal','') == 'account users':
        for p in a.get('privileges',[]):
            if p == 'CREATE_TABLE':
                print('CREATE_TABLE')
                break
" 2>/dev/null)
check "Bronze schema grants: CREATE_TABLE for account users" "$bronze_ct" "CREATE_TABLE"

echo ""

# ── Terraform State ──────────────────────────────────────────────────────────

echo "--- Terraform State ---"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESOURCE_COUNT=$(terraform -chdir="$SCRIPT_DIR" state list 2>/dev/null | wc -l | tr -d ' ')
if [[ "$RESOURCE_COUNT" -ge 95 ]]; then
  echo "  PASS: Terraform state has ${RESOURCE_COUNT} resources (expected >= 95)"
  PASS=$((PASS + 1))
else
  echo "  FAIL: Terraform state has ${RESOURCE_COUNT} resources (expected >= 95)"
  FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="
exit "$FAIL"
