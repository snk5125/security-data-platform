#!/usr/bin/env bash
# =============================================================================
# assemble-workloads.sh — Collect workload outputs into hub tfvars
# =============================================================================
# Iterates over all workload roots, runs `terraform output -json workload_manifest`
# in each, and assembles them into hub/workloads.auto.tfvars.json.
#
# Usage:
#   ./scripts/assemble-workloads.sh
#
# Prerequisites:
#   - All workload roots must be initialized and applied
#   - jq must be installed
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
HUB_DIR="$REPO_ROOT/hub"

if ! command -v jq &>/dev/null; then
  echo "ERROR: jq is required but not installed."
  exit 1
fi

workload_json="[]"
count=0

for dir in "$REPO_ROOT"/workloads/aws-workload-* \
           "$REPO_ROOT"/workloads/azure-workload-* \
           "$REPO_ROOT"/workloads/gcp-workload-*; do
  [[ -d "$dir" ]] || continue

  alias_name=$(basename "$dir")
  echo "  Collecting: $alias_name"

  output=$(cd "$dir" && terraform output -json workload_manifest 2>/dev/null) || {
    echo "  WARNING: Failed to read output from $alias_name (not applied?). Skipping."
    continue
  }

  workload_json=$(echo "$workload_json" | jq --argjson w "$output" '. + [$w]')
  count=$((count + 1))
done

if [[ "$count" -eq 0 ]]; then
  echo "ERROR: No workload outputs found. Apply at least one workload root first."
  exit 1
fi

jq -n --argjson w "$workload_json" '{"workloads": $w}' \
  > "$HUB_DIR/workloads.auto.tfvars.json"

echo ""
echo "Wrote $count workload(s) to hub/workloads.auto.tfvars.json"
