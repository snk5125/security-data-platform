#!/usr/bin/env bash
# =============================================================================
# migrate-state.sh — One-time state migration from environments/poc/
# =============================================================================
# Moves resources from the monolithic state into per-root states.
# Works with remote (S3) backends by pulling state locally, performing moves,
# then pushing back to the remote backends.
#
# Usage:
#   ./scripts/migrate-state.sh --dry-run    # Preview only
#   ./scripts/migrate-state.sh              # Execute migration
#
# Prerequisites:
#   - All new roots must be initialized (terraform init) with backends configured
#   - environments/poc/ state must be accessible
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DRY_RUN=false
BACKUP_DIR="$REPO_ROOT/.state-migration-backup"
WORK_DIR="$REPO_ROOT/.state-migration-work"

if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
  echo "=== DRY RUN MODE — no changes will be made ==="
  echo ""
fi

# ── Backup and pull states ───────────────────────────────────────────────────
if [[ "$DRY_RUN" == false ]]; then
  mkdir -p "$BACKUP_DIR" "$WORK_DIR"

  echo "Pulling and backing up states..."

  # Pull source state
  (cd "$REPO_ROOT/environments/poc" && terraform state pull > "$WORK_DIR/source.tfstate")
  cp "$WORK_DIR/source.tfstate" "$BACKUP_DIR/poc-$(date +%s).tfstate"
  echo "  Source state backed up to $BACKUP_DIR/"

  # Pull (empty) target states
  for root in "foundations/aws-security" "workloads/aws-workload-a" "workloads/aws-workload-b" "hub"; do
    slug="${root//\//-}"
    (cd "$REPO_ROOT/$root" && terraform state pull > "$WORK_DIR/$slug.tfstate")
  done
  echo "  All states pulled to $WORK_DIR/"
  echo ""
fi

# ── Migration mapping ──────────────────────────────────────────────────────
# Format: "source_address|target_root|target_address"
MIGRATIONS=(
  # Foundation root — S3 managed storage
  "module.security_account_baseline.aws_s3_bucket.managed_storage|foundations/aws-security|module.security_foundation.aws_s3_bucket.managed_storage"
  "module.security_account_baseline.aws_s3_bucket_versioning.managed_storage|foundations/aws-security|module.security_foundation.aws_s3_bucket_versioning.managed_storage"
  "module.security_account_baseline.aws_s3_bucket_server_side_encryption_configuration.managed_storage|foundations/aws-security|module.security_foundation.aws_s3_bucket_server_side_encryption_configuration.managed_storage"
  "module.security_account_baseline.aws_s3_bucket_public_access_block.managed_storage|foundations/aws-security|module.security_foundation.aws_s3_bucket_public_access_block.managed_storage"
  "module.security_account_baseline.aws_s3_bucket_policy.managed_storage|foundations/aws-security|module.security_foundation.aws_s3_bucket_policy.managed_storage"

  # Foundation root — SNS (absorbed from sns-alerts module)
  "module.sns_alerts.aws_sns_topic.alerts|foundations/aws-security|module.security_foundation.aws_sns_topic.alerts"
  "module.sns_alerts.aws_sns_topic_policy.alerts|foundations/aws-security|module.security_foundation.aws_sns_topic_policy.alerts"
  "module.sns_alerts.aws_iam_user.sns_publisher|foundations/aws-security|module.security_foundation.aws_iam_user.sns_publisher"
  "module.sns_alerts.aws_iam_user_policy.sns_publish|foundations/aws-security|module.security_foundation.aws_iam_user_policy.sns_publish"
  "module.sns_alerts.aws_iam_access_key.sns_publisher|foundations/aws-security|module.security_foundation.aws_iam_access_key.sns_publisher"

  # Workload A root
  "module.workload_a_baseline|workloads/aws-workload-a|module.baseline"
  "module.workload_a_data_sources|workloads/aws-workload-a|module.data_sources"

  # Workload B root
  "module.workload_b_baseline|workloads/aws-workload-b|module.baseline"
  "module.workload_b_data_sources|workloads/aws-workload-b|module.data_sources"

  # Hub root — IAM roles (inline in hub, not in a module)
  "module.security_account_baseline.aws_iam_role.managed_storage|hub|aws_iam_role.managed_storage"
  "module.security_account_baseline.aws_iam_role.hub|hub|aws_iam_role.hub"

  # Hub root — Databricks resources
  "module.cloud_integration|hub|module.cloud_integration"
  "module.unity_catalog|hub|module.unity_catalog"
  "module.workspace_config|hub|module.workspace_config"
  "module.bronze_ingestion|hub|module.jobs"
)

echo "Migration plan: ${#MIGRATIONS[@]} resource moves"
echo ""

for entry in "${MIGRATIONS[@]}"; do
  IFS='|' read -r source target_root target_addr <<< "$entry"
  slug="${target_root//\//-}"
  echo "  $source"
  echo "    → $target_root :: $target_addr"

  if [[ "$DRY_RUN" == false ]]; then
    terraform state mv \
      -state="$WORK_DIR/source.tfstate" \
      -state-out="$WORK_DIR/$slug.tfstate" \
      "$source" "$target_addr" || {
      echo "    ERROR: Failed to move $source"
      echo "    Rollback: cd environments/poc && terraform state push $BACKUP_DIR/poc-*.tfstate"
      rm -rf "$WORK_DIR"
      exit 1
    }
  fi
done

echo ""

# ── for_each address renames (cloud-integration external locations) ──────────
echo "Renaming for_each-keyed resources in hub state..."
RENAMES=(
  "module.cloud_integration.databricks_external_location.workload_a|module.cloud_integration.databricks_external_location.workload[\"workload-a\"]"
  "module.cloud_integration.databricks_external_location.workload_b|module.cloud_integration.databricks_external_location.workload[\"workload-b\"]"
)

for entry in "${RENAMES[@]}"; do
  IFS='|' read -r old_addr new_addr <<< "$entry"
  echo "  $old_addr → $new_addr"

  if [[ "$DRY_RUN" == false ]]; then
    terraform state mv \
      -state="$WORK_DIR/hub.tfstate" \
      -state-out="$WORK_DIR/hub.tfstate" \
      "$old_addr" "$new_addr" || {
      echo "    ERROR: Failed to rename $old_addr"
      rm -rf "$WORK_DIR"
      exit 1
    }
  fi
done

echo ""

# ── Push states to remote backends ──────────────────────────────────────────
if [[ "$DRY_RUN" == false ]]; then
  echo "Pushing migrated states to remote backends..."

  # Push modified source state back to environments/poc
  (cd "$REPO_ROOT/environments/poc" && terraform state push "$WORK_DIR/source.tfstate")
  echo "  environments/poc — pushed (resources removed)"

  # Push each target state
  for root in "foundations/aws-security" "workloads/aws-workload-a" "workloads/aws-workload-b" "hub"; do
    slug="${root//\//-}"
    (cd "$REPO_ROOT/$root" && terraform state push "$WORK_DIR/$slug.tfstate")
    echo "  $root — pushed"
  done

  echo ""

  # ── Validation ──────────────────────────────────────────────────────────────
  echo "Validating: terraform plan in each root (should show 0 changes)..."
  for root in "foundations/aws-security" "workloads/aws-workload-a" "workloads/aws-workload-b" "hub"; do
    echo "  Checking $root..."
    (cd "$REPO_ROOT/$root" && terraform plan -detailed-exitcode) || {
      echo "  WARNING: $root shows drift. Review before proceeding."
    }
  done

  # Clean up working directory
  rm -rf "$WORK_DIR"
fi

echo ""
echo "State migration complete."
echo "Backup is at: $BACKUP_DIR/"
echo "To rollback: cd environments/poc && terraform state push $BACKUP_DIR/poc-*.tfstate"
