#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# configure-cribl.sh
#
# Standalone script that applies Cribl Fleet (Edge) and Stream configurations
# via the Cribl REST API.  Uses the JSON config files under scripts/cribl-config/
# as the single source of truth — the same payloads the MCP tools would push.
#
# Usage:
#   export CRIBL_BASE_URL="https://your-cribl-instance:9000"
#   export CRIBL_CLIENT_ID="..."
#   export CRIBL_CLIENT_SECRET="..."
#   ./scripts/configure-cribl.sh              # apply everything
#   ./scripts/configure-cribl.sh --fleet-only  # fleet sources + pipeline only
#   ./scripts/configure-cribl.sh --stream-only # stream destinations + routes only
#   ./scripts/configure-cribl.sh --dry-run     # print what would be sent, no writes
#
# Required env vars:
#   CRIBL_BASE_URL       — base URL of the Cribl leader (e.g. https://main-<org>.cribl.cloud)
#   CRIBL_CLIENT_ID      — Cribl Cloud API client ID
#   CRIBL_CLIENT_SECRET  — Cribl Cloud API client secret
#
# Optional env vars:
#   CRIBL_AUTH_TOKEN     — if set, skip OAuth and use this bearer token directly
#   CRIBL_CLOUD_ORG      — Cribl Cloud org ID (auto-extracted from CRIBL_BASE_URL if omitted)
#
# Optional env vars (substituted into destination configs):
#   CRIBL_AWS_ACCESS_KEY_ID, CRIBL_AWS_SECRET_ACCESS_KEY,
#   S3_HOST_TELEMETRY_BUCKET, AWS_REGION,
#   AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET,
#   ADLS_STORAGE_ACCOUNT_NAME,
#   GCS_SERVICE_ACCOUNT_CREDENTIALS, GCS_HOST_TELEMETRY_BUCKET
# ---------------------------------------------------------------------------
set -euo pipefail

# ── Resolve script and config directories ──────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${SCRIPT_DIR}/cribl-config"
FLEET_DIR="${CONFIG_DIR}/fleet"
STREAM_DIR="${CONFIG_DIR}/stream"

# ── Parse flags ────────────────────────────────────────────────────────────

DRY_RUN=false
FLEET_ONLY=false
STREAM_ONLY=false

for arg in "$@"; do
  case "${arg}" in
    --dry-run)     DRY_RUN=true ;;
    --fleet-only)  FLEET_ONLY=true ;;
    --stream-only) STREAM_ONLY=true ;;
    *)
      echo "Unknown flag: ${arg}" >&2
      echo "Usage: $0 [--dry-run] [--fleet-only] [--stream-only]" >&2
      exit 1
      ;;
  esac
done

# ── Validate required env vars ─────────────────────────────────────────────

if [[ -z "${CRIBL_BASE_URL:-}" ]]; then
  echo "ERROR: CRIBL_BASE_URL is not set." >&2
  exit 1
fi

# Strip trailing slash from base URL for consistency
CRIBL_BASE_URL="${CRIBL_BASE_URL%/}"

# ── OAuth token acquisition ──────────────────────────────────────────────
# If CRIBL_AUTH_TOKEN is already set, use it directly (allows manual override).
# Otherwise, obtain a token via OAuth client_credentials flow against Cribl Cloud.

get_token() {
  local response
  response=$(curl -s -S -X POST "https://login.cribl.cloud/oauth/token" \
    -H "Content-Type: application/json" \
    -d "{
      \"grant_type\": \"client_credentials\",
      \"client_id\": \"${CRIBL_CLIENT_ID}\",
      \"client_secret\": \"${CRIBL_CLIENT_SECRET}\",
      \"audience\": \"https://api.cribl.cloud\"
    }")

  local token
  token=$(echo "${response}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)

  if [[ -z "${token}" ]]; then
    echo "ERROR: Failed to obtain OAuth token from Cribl Cloud." >&2
    echo "Response: ${response}" >&2
    exit 1
  fi

  echo "${token}"
}

if [[ -n "${CRIBL_AUTH_TOKEN:-}" ]]; then
  echo "Using provided CRIBL_AUTH_TOKEN (skipping OAuth)."
else
  # OAuth flow requires client credentials
  if [[ -z "${CRIBL_CLIENT_ID:-}" || -z "${CRIBL_CLIENT_SECRET:-}" ]]; then
    echo "ERROR: Either CRIBL_AUTH_TOKEN or both CRIBL_CLIENT_ID and CRIBL_CLIENT_SECRET must be set." >&2
    exit 1
  fi

  echo "Obtaining OAuth token from Cribl Cloud..."
  CRIBL_AUTH_TOKEN="$(get_token)"
  echo "OAuth token obtained successfully."
fi

# ── Helper: authenticated REST call ───────────────────────────────────────

cribl_api() {
  local method="$1"
  local path="$2"
  local data="${3:-}"

  local url="${CRIBL_BASE_URL}${path}"

  if [[ "${DRY_RUN}" == "true" ]]; then
    echo "[DRY-RUN] ${method} ${url}"
    if [[ -n "${data}" ]]; then
      echo "${data}" | python3 -m json.tool 2>/dev/null || echo "${data}"
    fi
    return 0
  fi

  local args=( -s -S -w "\n%{http_code}" -X "${method}" )
  args+=( -H "Authorization: Bearer ${CRIBL_AUTH_TOKEN}" )
  args+=( -H "Content-Type: application/json" )

  if [[ -n "${data}" ]]; then
    args+=( -d "${data}" )
  fi

  local response
  response="$(curl "${args[@]}" "${url}")"

  # Last line is the HTTP status code
  local http_code
  http_code="$(echo "${response}" | tail -1)"
  local body
  body="$(echo "${response}" | sed '$d')"

  echo "${body}"
  # Return non-zero for 4xx/5xx responses (but not 409 which we handle)
  if [[ "${http_code}" -ge 400 && "${http_code}" -ne 409 ]]; then
    echo "  -> HTTP ${http_code}" >&2
    return 1
  fi
  return 0
}

# ── Helper: substitute env vars in a JSON config file ─────────────────────

envsubst_json() {
  local file="$1"

  # Use envsubst to replace ${VAR} placeholders with env var values.
  # Only substitute variables that are actually set to avoid blanking configs.
  envsubst < "${file}"
}

# ── Apply a pipeline to a fleet/worker group ──────────────────────────────

apply_pipeline() {
  local group="$1"
  local config_file="$2"

  local pipeline_id
  pipeline_id="$(python3 -c "import json,sys; print(json.load(sys.stdin)['id'])" < "${config_file}")"

  echo ">> Applying pipeline '${pipeline_id}' to group '${group}'..."

  local payload
  payload="$(cat "${config_file}")"

  # Try PATCH first (update existing), fall back to POST (create new)
  local result
  if result="$(cribl_api PATCH "/api/v1/m/${group}/pipelines/${pipeline_id}" "${payload}" 2>&1)"; then
    echo "   Updated pipeline '${pipeline_id}'."
  else
    echo "   PATCH failed, attempting POST to create..."
    if result="$(cribl_api POST "/api/v1/m/${group}/pipelines" "${payload}" 2>&1)"; then
      echo "   Created pipeline '${pipeline_id}'."
    else
      echo "   ERROR: Failed to apply pipeline '${pipeline_id}'." >&2
      echo "   ${result}" >&2
      return 1
    fi
  fi
}

# ── Apply a source to a fleet/worker group ────────────────────────────────

apply_source() {
  local group="$1"
  local config_file="$2"

  local source_id source_type
  source_id="$(python3 -c "import json,sys; d=json.load(sys.stdin); print(d['id'])" < "${config_file}")"
  source_type="$(python3 -c "import json,sys; d=json.load(sys.stdin); print(d['type'])" < "${config_file}")"

  echo ">> Applying source '${source_id}' (type: ${source_type}) to group '${group}'..."

  local payload
  payload="$(cat "${config_file}")"

  local result
  if result="$(cribl_api PATCH "/api/v1/m/${group}/lib/sources/${source_type}/${source_id}" "${payload}" 2>&1)"; then
    echo "   Updated source '${source_id}'."
  else
    echo "   PATCH failed, attempting POST to create..."
    if result="$(cribl_api POST "/api/v1/m/${group}/lib/sources/${source_type}" "${payload}" 2>&1)"; then
      echo "   Created source '${source_id}'."
    else
      echo "   ERROR: Failed to apply source '${source_id}'." >&2
      echo "   ${result}" >&2
      return 1
    fi
  fi
}

# ── Apply a destination to a stream worker group ─────────────────────────

apply_destination() {
  local group="$1"
  local config_file="$2"

  local dest_id dest_type
  dest_id="$(python3 -c "import json,sys; d=json.load(sys.stdin); print(d['id'])" < "${config_file}")"
  dest_type="$(python3 -c "import json,sys; d=json.load(sys.stdin); print(d['type'])" < "${config_file}")"

  echo ">> Applying destination '${dest_id}' (type: ${dest_type}) to group '${group}'..."

  # Substitute credential env vars into the config
  local payload
  payload="$(envsubst_json "${config_file}")"

  local result
  if result="$(cribl_api PATCH "/api/v1/m/${group}/lib/destinations/${dest_type}/${dest_id}" "${payload}" 2>&1)"; then
    echo "   Updated destination '${dest_id}'."
  else
    echo "   PATCH failed, attempting POST to create..."
    if result="$(cribl_api POST "/api/v1/m/${group}/lib/destinations/${dest_type}" "${payload}" 2>&1)"; then
      echo "   Created destination '${dest_id}'."
    else
      echo "   ERROR: Failed to apply destination '${dest_id}'." >&2
      echo "   ${result}" >&2
      return 1
    fi
  fi
}

# ── Commit and deploy a worker group ──────────────────────────────────────

commit_and_deploy() {
  local group="$1"
  local message="${2:-Automated config push via configure-cribl.sh}"

  echo ">> Committing config for group '${group}'..."
  cribl_api POST "/api/v1/m/${group}/version/commit" "{\"message\": \"${message}\"}" >/dev/null

  echo ">> Deploying group '${group}'..."
  cribl_api PATCH "/api/v1/m/${group}/deploy" "{}" >/dev/null

  echo "   Deployed '${group}' successfully."
}

# ── Main: Fleet configuration ─────────────────────────────────────────────

apply_fleet() {
  local fleet_group="default_fleet"

  echo ""
  echo "============================================"
  echo " Fleet Configuration (group: ${fleet_group})"
  echo "============================================"

  # Apply pipeline
  apply_pipeline "${fleet_group}" "${FLEET_DIR}/pipelines/bash_history_obfuscation.json"

  # Apply all sources
  for source_file in "${FLEET_DIR}"/sources/*.json; do
    apply_source "${fleet_group}" "${source_file}"
  done

  # Commit and deploy
  commit_and_deploy "${fleet_group}" "Apply fleet sources and pipelines"
}

# ── Main: Stream configuration ────────────────────────────────────────────

apply_stream() {
  local stream_group="default"

  echo ""
  echo "============================================"
  echo " Stream Configuration (group: ${stream_group})"
  echo "============================================"

  # Apply all destinations
  for dest_file in "${STREAM_DIR}"/destinations/*.json; do
    apply_destination "${stream_group}" "${dest_file}"
  done

  # Apply route
  echo ">> Applying route 'edge_to_cloud' to group '${stream_group}'..."
  local route_payload
  route_payload="$(envsubst_json "${STREAM_DIR}/routes/edge_to_cloud.json")"

  local route_id
  route_id="$(echo "${route_payload}" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")"

  local result
  if result="$(cribl_api PATCH "/api/v1/m/${stream_group}/lib/routes/${route_id}" "${route_payload}" 2>&1)"; then
    echo "   Updated route '${route_id}'."
  else
    echo "   PATCH failed, attempting POST to create..."
    if result="$(cribl_api POST "/api/v1/m/${stream_group}/lib/routes" "${route_payload}" 2>&1)"; then
      echo "   Created route '${route_id}'."
    else
      echo "   ERROR: Failed to apply route '${route_id}'." >&2
      echo "   ${result}" >&2
      return 1
    fi
  fi

  # Commit and deploy
  commit_and_deploy "${stream_group}" "Apply stream destinations and routes"
}

# ── Entry point ────────────────────────────────────────────────────────────

echo "Cribl Configuration Script"
echo "Base URL: ${CRIBL_BASE_URL}"
echo "Dry run:  ${DRY_RUN}"
echo ""

if [[ "${STREAM_ONLY}" != "true" ]]; then
  apply_fleet
fi

if [[ "${FLEET_ONLY}" != "true" ]]; then
  apply_stream
fi

echo ""
echo "Done."
