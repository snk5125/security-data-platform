#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Phase 8: Bronze Layer Ingestion Validation
# -----------------------------------------------------------------------------
# Verifies that Phase 8 resources (notebooks, jobs) were created correctly
# in the Databricks workspace.
#
# Prerequisites:
#   - DATABRICKS_HOST and DATABRICKS_TOKEN environment variables set, OR
#   - Pass workspace URL and PAT as arguments:
#       ./validate-phase8.sh https://dbc-xxx.cloud.databricks.com dapi...
#
# Exit code 0 = all checks passed, non-zero = failure.
# -----------------------------------------------------------------------------
set -uo pipefail

DATABRICKS_HOST="${1:-${DATABRICKS_HOST:-}}"
DATABRICKS_TOKEN="${2:-${DATABRICKS_TOKEN:-}}"

if [[ -z "$DATABRICKS_HOST" || -z "$DATABRICKS_TOKEN" ]]; then
  echo "Usage: $0 <workspace-url> <pat>"
  echo "  or set DATABRICKS_HOST and DATABRICKS_TOKEN environment variables"
  exit 1
fi

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

echo "=== Phase 8: Bronze Layer Ingestion Validation ==="
echo ""

# ── Notebooks ──────────────────────────────────────────────────────────────

echo "--- Notebooks ---"

NOTEBOOK_BASE="/Shared/security-lakehouse/bronze"

for nb in 01_bronze_cloudtrail 02_bronze_vpc_flow 03_bronze_guardduty 04_bronze_config; do
  status=$(db_api GET "/api/2.0/workspace/get-status" "{\"path\": \"${NOTEBOOK_BASE}/${nb}\"}")
  obj_type=$(echo "$status" | python3 -c "import sys,json; print(json.load(sys.stdin).get('object_type',''))" 2>/dev/null)
  check "Notebook '${nb}' exists" "$obj_type" "NOTEBOOK"
done

echo ""

# ── Jobs ────────────────────────────────────────────────────────────────────

echo "--- Jobs ---"

jobs=$(db_api GET "/api/2.1/jobs/list?limit=20")

for job_name in bronze-cloudtrail-ingest bronze-vpc-flow-ingest bronze-guardduty-ingest bronze-config-ingest; do
  found=$(echo "$jobs" | python3 -c "
import sys,json
data = json.load(sys.stdin)
for j in data.get('jobs',[]):
    if j.get('settings',{}).get('name','') == '${job_name}':
        print('found')
        break
" 2>/dev/null)
  check "Job '${job_name}' exists" "$found" "found"
done

# Check that all jobs are PAUSED
paused_count=$(echo "$jobs" | python3 -c "
import sys,json
data = json.load(sys.stdin)
count = 0
for j in data.get('jobs',[]):
    name = j.get('settings',{}).get('name','')
    if name.startswith('bronze-') and name.endswith('-ingest'):
        sched = j.get('settings',{}).get('schedule',{})
        if sched.get('pause_status','') == 'PAUSED':
            count += 1
print(count)
" 2>/dev/null)
check "All 4 bronze jobs are PAUSED" "$paused_count" "4"

# Check that jobs use serverless (environments list includes "Default")
# The list endpoint returns environments but not tasks — use environments field.
serverless_count=$(echo "$jobs" | python3 -c "
import sys,json
data = json.load(sys.stdin)
count = 0
for j in data.get('jobs',[]):
    name = j.get('settings',{}).get('name','')
    if name.startswith('bronze-') and name.endswith('-ingest'):
        envs = j.get('settings',{}).get('environments',[])
        for e in envs:
            if e.get('environment_key','') == 'Default':
                count += 1
                break
print(count)
" 2>/dev/null)
check "All 4 bronze jobs use serverless compute" "$serverless_count" "4"

# Check that jobs have correct notebook parameters by fetching each job detail.
# The list endpoint does not return task details — must use the get endpoint.
job_ids=$(echo "$jobs" | python3 -c "
import sys,json
data = json.load(sys.stdin)
for j in data.get('jobs',[]):
    name = j.get('settings',{}).get('name','')
    if name.startswith('bronze-') and name.endswith('-ingest'):
        print(j.get('job_id',''))
" 2>/dev/null)

param_ok=0
for jid in $job_ids; do
  job_detail=$(db_api GET "/api/2.1/jobs/get?job_id=${jid}")
  has_params=$(echo "$job_detail" | python3 -c "
import sys,json
data = json.load(sys.stdin)
tasks = data.get('settings',{}).get('tasks',[])
for t in tasks:
    params = t.get('notebook_task',{}).get('base_parameters',{})
    if 'workload_a_bucket' in params and 'workload_b_bucket' in params and 'checkpoint_base' in params:
        print('yes')
        break
" 2>/dev/null)
  if [[ "$has_params" == "yes" ]]; then
    param_ok=$((param_ok + 1))
  fi
done
check "All 4 jobs have correct notebook parameters" "$param_ok" "4"

echo ""

# ── Terraform State ────────────────────────────────────────────────────────

echo "--- Terraform State ---"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESOURCE_COUNT=$(terraform -chdir="$SCRIPT_DIR" state list 2>/dev/null | wc -l | tr -d ' ')
if [[ "$RESOURCE_COUNT" -ge 107 ]]; then
  echo "  PASS: Terraform state has ${RESOURCE_COUNT} resources (expected >= 107)"
  PASS=$((PASS + 1))
else
  echo "  FAIL: Terraform state has ${RESOURCE_COUNT} resources (expected >= 107)"
  FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="
exit "$FAIL"
