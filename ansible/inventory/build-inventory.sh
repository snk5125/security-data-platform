#!/usr/bin/env bash
# =============================================================================
# build-inventory.sh — Generate Ansible inventory from Terraform workload state
# =============================================================================
# Walks all workload roots (aws-workload-*, azure-workload-*, gcp-workload-*),
# runs `terraform output -json` in each, and builds an Ansible JSON inventory
# file at ansible/inventory/hosts.json.
#
# Also extracts SSH private keys into .keys/<workload-name>.pem (chmod 600)
# for Linux host access.
#
# Usage:
#   cd ansible/inventory && bash build-inventory.sh
#   # — or —
#   bash ansible/inventory/build-inventory.sh   (from repo root)
#
# Prerequisites:
#   - All workload roots must be initialized and applied
#   - jq must be installed
#   - Terraform must be installed
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
KEYS_DIR="$SCRIPT_DIR/.keys"
INVENTORY_FILE="$SCRIPT_DIR/hosts.json"

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
if ! command -v jq &>/dev/null; then
  echo "ERROR: jq is required but not installed."
  exit 1
fi

if ! command -v terraform &>/dev/null; then
  echo "ERROR: terraform is required but not installed."
  exit 1
fi

# ---------------------------------------------------------------------------
# Prepare key directory
# ---------------------------------------------------------------------------
mkdir -p "$KEYS_DIR"
chmod 700 "$KEYS_DIR"

# ---------------------------------------------------------------------------
# Cloud-specific SSH user mapping (Linux)
# ---------------------------------------------------------------------------
ssh_user_for_cloud() {
  case "$1" in
    aws)   echo "ec2-user" ;;
    azure) echo "azureadmin" ;;
    gcp)   echo "admin" ;;
    *)     echo "ERROR: Unknown cloud '$1'" >&2; exit 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# Cloud-specific Windows admin user mapping
# AWS uses the built-in Administrator account (password set via user_data).
# Azure uses the admin_username from the VM definition (azureadmin).
# GCP creates a custom "cribl_admin" user via startup script because GCP
# Windows images do not enable the built-in Administrator account.
# ---------------------------------------------------------------------------
windows_user_for_cloud() {
  case "$1" in
    aws)   echo "Administrator" ;;
    azure) echo "azureadmin" ;;
    gcp)   echo "cribl_admin" ;;
    *)     echo "ERROR: Unknown cloud '$1'" >&2; exit 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# Initialize inventory JSON structure
# ---------------------------------------------------------------------------
# Ansible JSON inventory format:
#   { "group": { "hosts": [...], "children": [...] }, "_meta": { "hostvars": {} } }
#
# Groups:
#   linux        → children: [aws_linux, azure_linux, gcp_linux]
#   windows      → children: [aws_windows, azure_windows, gcp_windows]
#   aws_linux    → hosts: [...]
#   azure_linux  → hosts: [...]
#   gcp_linux    → hosts: [...]
#   aws_windows  → hosts: [...]
#   azure_windows→ hosts: [...]
#   gcp_windows  → hosts: [...]
# ---------------------------------------------------------------------------
inventory=$(cat <<'INVENTORY_TEMPLATE'
{
  "linux": {
    "children": { "aws_linux": {}, "azure_linux": {}, "gcp_linux": {} }
  },
  "windows": {
    "children": { "aws_windows": {}, "azure_windows": {}, "gcp_windows": {} }
  },
  "aws_linux":      { "hosts": {} },
  "azure_linux":    { "hosts": {} },
  "gcp_linux":      { "hosts": {} },
  "aws_windows":    { "hosts": {} },
  "azure_windows":  { "hosts": {} },
  "gcp_windows":    { "hosts": {} }
}
INVENTORY_TEMPLATE
)

count=0

# ---------------------------------------------------------------------------
# Walk workload directories and collect outputs
# ---------------------------------------------------------------------------
for dir in "$REPO_ROOT"/workloads/aws-workload-* \
           "$REPO_ROOT"/workloads/azure-workload-* \
           "$REPO_ROOT"/workloads/gcp-workload-*; do

  # Skip if glob didn't match any real directory
  [[ -d "$dir" ]] || continue

  # Skip template directories
  [[ "$(basename "$dir")" == _template-* ]] && continue

  workload_name=$(basename "$dir")
  echo "  Collecting: $workload_name"

  # -------------------------------------------------------------------------
  # Get all outputs as JSON (terraform output -json returns all root outputs)
  # -------------------------------------------------------------------------
  all_outputs=$(cd "$dir" && terraform output -json 2>/dev/null) || {
    echo "  WARNING: Failed to read outputs from $workload_name (not applied?). Skipping."
    continue
  }

  # -------------------------------------------------------------------------
  # Extract cloud type and workload alias from the manifest
  # -------------------------------------------------------------------------
  cloud=$(echo "$all_outputs" | jq -r '.workload_manifest.value.cloud // empty')
  alias=$(echo "$all_outputs" | jq -r '.workload_manifest.value.alias // empty')

  if [[ -z "$cloud" || -z "$alias" ]]; then
    echo "  WARNING: Missing cloud or alias in manifest for $workload_name. Skipping."
    continue
  fi

  # -------------------------------------------------------------------------
  # Extract SSH private key and write to .keys/<workload>.pem
  # Terraform sensitive outputs require -raw or explicit -json to reveal.
  # -------------------------------------------------------------------------
  ssh_key=$(cd "$dir" && terraform output -raw ssh_private_key 2>/dev/null) || {
    echo "  WARNING: No ssh_private_key output for $workload_name. Linux host will be skipped."
    ssh_key=""
  }

  if [[ -n "$ssh_key" ]]; then
    key_file="$KEYS_DIR/${workload_name}.pem"
    echo "$ssh_key" > "$key_file"
    chmod 600 "$key_file"
  fi

  # -------------------------------------------------------------------------
  # Extract Windows admin password for WinRM authentication
  # Terraform sensitive outputs require -raw to reveal the value.
  # -------------------------------------------------------------------------
  windows_password=$(cd "$dir" && terraform output -raw windows_admin_password 2>/dev/null) || {
    echo "  WARNING: No windows_admin_password output for $workload_name. Windows host will lack credentials."
    windows_password=""
  }

  # -------------------------------------------------------------------------
  # Extract Linux and Windows public IPs
  # GCP uses linux_vm_ip / windows_vm_ip; AWS and Azure use linux_public_ip / windows_public_ip
  # -------------------------------------------------------------------------
  if [[ "$cloud" == "gcp" ]]; then
    linux_ip=$(echo "$all_outputs" | jq -r '.linux_vm_ip.value // empty')
    windows_ip=$(echo "$all_outputs" | jq -r '.windows_vm_ip.value // empty')
  else
    linux_ip=$(echo "$all_outputs" | jq -r '.linux_public_ip.value // empty')
    windows_ip=$(echo "$all_outputs" | jq -r '.windows_public_ip.value // empty')
  fi

  ssh_user=$(ssh_user_for_cloud "$cloud")

  # -------------------------------------------------------------------------
  # Add Linux host to inventory
  # -------------------------------------------------------------------------
  if [[ -n "$linux_ip" && -n "$ssh_key" ]]; then
    linux_hostname="${workload_name}-linux"

    # Add host with vars directly in the hosts dict (static JSON inventory format)
    inventory=$(echo "$inventory" | jq \
      --arg group "${cloud}_linux" \
      --arg host "$linux_hostname" \
      --arg ip "$linux_ip" \
      --arg user "$ssh_user" \
      --arg keyfile "$key_file" \
      --arg cloud "$cloud" \
      --arg workload "$alias" \
      '.[$group].hosts[$host] = {
        "ansible_host": $ip,
        "ansible_user": $user,
        "ansible_ssh_private_key_file": $keyfile,
        "cribl_cloud": $cloud,
        "cribl_workload": $workload
      }')

    echo "    + $linux_hostname ($linux_ip)"
  else
    echo "    - Skipping Linux host (no IP or no SSH key)"
  fi

  # -------------------------------------------------------------------------
  # Add Windows host to inventory
  # Includes ansible_user and ansible_password for WinRM authentication.
  # -------------------------------------------------------------------------
  if [[ -n "$windows_ip" ]]; then
    windows_hostname="${workload_name}-windows"
    windows_user=$(windows_user_for_cloud "$cloud")

    # Add host with vars directly in the hosts dict (static JSON inventory format)
    inventory=$(echo "$inventory" | jq \
      --arg group "${cloud}_windows" \
      --arg host "$windows_hostname" \
      --arg ip "$windows_ip" \
      --arg user "$windows_user" \
      --arg password "$windows_password" \
      --arg cloud "$cloud" \
      --arg workload "$alias" \
      '.[$group].hosts[$host] = {
        "ansible_host": $ip,
        "ansible_user": $user,
        "ansible_password": $password,
        "cribl_cloud": $cloud,
        "cribl_workload": $workload
      }')

    echo "    + $windows_hostname ($windows_ip)"
  else
    echo "    - Skipping Windows host (no IP)"
  fi

  count=$((count + 1))
done

# ---------------------------------------------------------------------------
# Write inventory file
# ---------------------------------------------------------------------------
if [[ "$count" -eq 0 ]]; then
  echo ""
  echo "ERROR: No workload outputs found. Apply at least one workload root first."
  exit 1
fi

echo "$inventory" | jq '.' > "$INVENTORY_FILE"

echo ""
echo "Wrote inventory for $count workload(s) to $INVENTORY_FILE"
echo "SSH keys written to $KEYS_DIR/"
