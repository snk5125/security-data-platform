#!/usr/bin/env bash
# =============================================================================
# apply-all.sh — Full deployment sequence for all roots
# =============================================================================
# Applies all 4 roots in dependency order. Bootstrap is skipped if already
# applied (checks for existing state bucket).
#
# Usage:
#   ./scripts/apply-all.sh              # Interactive (prompts before each apply)
#   ./scripts/apply-all.sh --auto       # Auto-approve all applies (CI/CD)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
APPROVE_FLAG=""

if [[ "${1:-}" == "--auto" ]]; then
  APPROVE_FLAG="-auto-approve"
fi

echo "============================================================"
echo "  Security Data Lakehouse — Full Deploy"
echo "============================================================"
echo ""

# ── Step 1: Bootstrap ──────────────────────────────────────────────────────
echo "=== Step 1/4: Bootstrap ==="
(cd "$REPO_ROOT/bootstrap" && terraform init && terraform apply $APPROVE_FLAG)
echo ""

# ── Step 2: Foundation ─────────────────────────────────────────────────────
echo "=== Step 2/4: Foundation (aws-security) ==="
(cd "$REPO_ROOT/foundations/aws-security" && terraform init && terraform apply $APPROVE_FLAG)
echo ""

# ── Step 3: Workloads (parallel) ───────────────────────────────────────────
echo "=== Step 3/4: Workloads ==="
pids=()
dirs=()
for dir in "$REPO_ROOT"/workloads/aws-workload-* \
           "$REPO_ROOT"/workloads/azure-workload-*; do
  [[ -d "$dir" ]] || continue
  [[ -f "$dir/main.tf" ]] || continue
  echo "  Starting: $(basename "$dir")"
  (cd "$dir" && terraform init && terraform apply $APPROVE_FLAG) &
  pids+=($!)
  dirs+=("$dir")
done

failed=0
for i in "${!pids[@]}"; do
  if ! wait "${pids[$i]}"; then
    echo "  ERROR: $(basename "${dirs[$i]}") apply failed."
    failed=1
  fi
done

if [[ "$failed" -eq 1 ]]; then
  echo "ERROR: One or more workload applies failed. Fix and re-run."
  exit 1
fi
echo ""

# ── Step 4: Assemble + Hub ─────────────────────────────────────────────────
echo "=== Step 4/4: Assemble workloads + Hub ==="
"$SCRIPT_DIR/assemble-workloads.sh"
(cd "$REPO_ROOT/hub" && terraform init && terraform apply $APPROVE_FLAG)
echo ""

echo "============================================================"
echo "  All roots applied successfully."
echo "============================================================"
