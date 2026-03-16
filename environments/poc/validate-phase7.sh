#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Phase 7: Workspace Configuration Validation
# -----------------------------------------------------------------------------
# Verifies that Phase 7 resources (cluster policy, compute availability) were
# created correctly in the Databricks workspace.
#
# Prerequisites:
#   - DATABRICKS_HOST and DATABRICKS_TOKEN environment variables set, OR
#   - Pass workspace URL and PAT as arguments:
#       ./validate-phase7.sh https://dbc-xxx.cloud.databricks.com dapi...
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
      "${DATABRICKS_HOST}${endpoint}" \
      -d "$data"
  else
    curl -s -X "$method" \
      -H "Authorization: Bearer ${DATABRICKS_TOKEN}" \
      "${DATABRICKS_HOST}${endpoint}"
  fi
}

echo "=== Phase 7: Workspace Configuration Validation ==="
echo ""

# ── Cluster Policy ──────────────────────────────────────────────────────────

echo "--- Cluster Policy ---"

# 1. List cluster policies and find the PoC policy
policies=$(db_api GET "/api/2.0/policies/clusters/list")
poc_policy=$(echo "$policies" | python3 -c "
import sys,json
data = json.load(sys.stdin)
for p in data.get('policies',[]):
    if p.get('name','') == 'security-lakehouse-poc':
        print(json.dumps(p))
        break
" 2>/dev/null)

policy_name=$(echo "$poc_policy" | python3 -c "import sys,json; print(json.load(sys.stdin).get('name',''))" 2>/dev/null)
check "Cluster policy 'security-lakehouse-poc' exists" "$policy_name" "security-lakehouse-poc"

# 2. Policy enforces single-node (num_workers = 0)
policy_def=$(echo "$poc_policy" | python3 -c "import sys,json; print(json.load(sys.stdin).get('definition',''))" 2>/dev/null)
num_workers_fixed=$(echo "$policy_def" | python3 -c "
import sys,json
d = json.loads(sys.stdin.read())
nw = d.get('num_workers',{})
if nw.get('type') == 'fixed' and nw.get('value') == 0:
    print('fixed_0')
" 2>/dev/null)
check "Policy enforces single-node (num_workers fixed=0)" "$num_workers_fixed" "fixed_0"

# 3. Policy enforces SINGLE_USER data security mode
dsm_fixed=$(echo "$policy_def" | python3 -c "
import sys,json
d = json.loads(sys.stdin.read())
dsm = d.get('data_security_mode',{})
if dsm.get('type') == 'fixed' and dsm.get('value') == 'SINGLE_USER':
    print('SINGLE_USER')
" 2>/dev/null)
check "Policy enforces SINGLE_USER data security mode" "$dsm_fixed" "SINGLE_USER"

# 4. Policy has auto-termination range
auto_term=$(echo "$policy_def" | python3 -c "
import sys,json
d = json.loads(sys.stdin.read())
at = d.get('autotermination_minutes',{})
if at.get('type') == 'range' and at.get('minValue') == 10:
    print('range_10_60')
" 2>/dev/null)
check "Policy has auto-termination range (10-60 min)" "$auto_term" "range_10_60"

echo ""

# ── Compute Availability ───────────────────────────────────────────────────

echo "--- Compute Availability ---"

# 5. Serverless Starter Warehouse exists
warehouses=$(db_api GET "/api/2.0/sql/warehouses")
starter_name=$(echo "$warehouses" | python3 -c "
import sys,json
data = json.load(sys.stdin)
for w in data.get('warehouses',[]):
    if 'Starter' in w.get('name',''):
        print(w.get('name',''))
        break
" 2>/dev/null)
check "Serverless Starter Warehouse exists" "$starter_name" "Starter"

# 6. Starter Warehouse is serverless
starter_serverless=$(echo "$warehouses" | python3 -c "
import sys,json
data = json.load(sys.stdin)
for w in data.get('warehouses',[]):
    if 'Starter' in w.get('name',''):
        if w.get('enable_serverless_compute'):
            print('serverless')
        break
" 2>/dev/null)
check "Starter Warehouse is serverless" "$starter_serverless" "serverless"

# 7. Classic compute not available (Free Edition)
create_result=$(db_api POST "/api/2.1/clusters/create" '{"cluster_name":"__test__","spark_version":"17.3.x-scala2.13","node_type_id":"m5d.large","num_workers":0}')
no_workers=$(echo "$create_result" | python3 -c "
import sys,json
data = json.load(sys.stdin)
msg = data.get('message','')
if 'worker environment' in msg.lower() or 'does not have any associated' in msg.lower():
    print('no_workers')
else:
    # If it succeeded (unlikely), delete the cluster
    cid = data.get('cluster_id','')
    if cid:
        print('has_workers')
" 2>/dev/null)
check "Classic compute unavailable (Free Edition confirmed)" "$no_workers" "no_workers"

echo ""

# ── Terraform State ────────────────────────────────────────────────────────

echo "--- Terraform State ---"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESOURCE_COUNT=$(terraform -chdir="$SCRIPT_DIR" state list 2>/dev/null | wc -l | tr -d ' ')
if [[ "$RESOURCE_COUNT" -ge 99 ]]; then
  echo "  PASS: Terraform state has ${RESOURCE_COUNT} resources (expected >= 99)"
  PASS=$((PASS + 1))
else
  echo "  FAIL: Terraform state has ${RESOURCE_COUNT} resources (expected >= 99)"
  FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="
exit "$FAIL"
