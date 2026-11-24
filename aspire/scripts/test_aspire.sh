#!/usr/bin/env bash
# Smoke test for local-infra Aspire Dashboard stack.
# Verifies container status, Web UI HTTP 200, and OTLP HTTP /v1/traces, /v1/logs, /v1/metrics endpoints.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SERVICE_DIR/.." && pwd)"
cd "$SERVICE_DIR"

# Reuse deterministic stack hash calculation from repo-wide helper
# shellcheck source=../../scripts/compose_hash.sh
source "$REPO_ROOT/scripts/compose_hash.sh"

# SSOT: local .env in aspire/ directory
ENV_FILE="$SERVICE_DIR/.env"
[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE (copy .env.example)" >&2; exit 1; }
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

: "${ASPIRE_CONNECT_IP:?ASPIRE_CONNECT_IP must be set in .env}"
: "${ASPIRE_UI_PORT:?ASPIRE_UI_PORT must be set in .env}"
: "${ASPIRE_OTLP_HTTP_PORT:?ASPIRE_OTLP_HTTP_PORT must be set in .env}"

CONTAINER_NAME="aspire"
UI_URL="http://${ASPIRE_CONNECT_IP}:${ASPIRE_UI_PORT}"
OTLP_URL="http://${ASPIRE_CONNECT_IP}:${ASPIRE_OTLP_HTTP_PORT}"

# Detect which container runtime owns the aspire container (Podman first, Docker fallback)
CONTAINER_CLI=""
if command -v podman >/dev/null 2>&1 && podman inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  CONTAINER_CLI="podman"
elif command -v docker >/dev/null 2>&1 && docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  CONTAINER_CLI="docker"
elif command -v podman >/dev/null 2>&1; then
  CONTAINER_CLI="podman"
elif command -v docker >/dev/null 2>&1; then
  CONTAINER_CLI="docker"
else
  echo "ERROR: Neither 'podman' nor 'docker' is installed." >&2
  exit 1
fi

echo "=========================================================="
echo " Running Aspire Dashboard Smoke Tests"
echo " Runtime:         $CONTAINER_CLI"
echo " Target Web UI:   $UI_URL"
echo " Target OTLP:     $OTLP_URL"
echo "=========================================================="

echo "=== 0. Verifying Compose & .env Hash State ==="
CURRENT_HASH="$(compute_stack_hash "$SERVICE_DIR")"
echo "  [aspire sha256]: $CURRENT_HASH"
if [ -f "$SERVICE_DIR/.compose.sha256" ]; then
  SAVED_HASH="$(cat "$SERVICE_DIR/.compose.sha256" 2>/dev/null || true)"
  if [ "$CURRENT_HASH" != "$SAVED_HASH" ]; then
    echo "  WARNING: aspire Compose/.env changed since last startup ($SAVED_HASH -> $CURRENT_HASH)."
    echo "           Run 'bash scripts/run_aspire.sh' to apply changes."
  else
    echo "  [aspire hash-check]: Matches active stack state (.compose.sha256)"
  fi
fi

echo ""
echo "=== 1. Checking Container Status ==="
STATUS="$("$CONTAINER_CLI" inspect --format '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null || echo "not running")"
echo "  [$CONTAINER_NAME status]: $STATUS"
if [[ "$STATUS" != "running" ]]; then
  echo "ERROR: Container $CONTAINER_NAME is not running (status: $STATUS)" >&2
  exit 1
fi

echo ""
echo "=== 2. Checking Web UI Endpoint ==="
UI_STATUS="$(curl -sL --max-time 5 --connect-timeout 2 -o /dev/null -w "%{http_code}" "$UI_URL" || echo "failed")"
echo "  GET $UI_URL -> HTTP $UI_STATUS"
if [[ "$UI_STATUS" != "200" ]]; then
  echo "ERROR: Aspire Web UI check failed with HTTP $UI_STATUS (expected 200)" >&2
  exit 1
fi

check_otlp_endpoint() {
  local step_num="$1"
  local label="$2"
  local path="$3"
  local status="failed"

  echo ""
  echo "=== ${step_num}. Checking OTLP ${label} Ingestion Endpoint ==="
  for _ in {1..15}; do
    local code
    code="$(curl -s --max-time 5 --connect-timeout 2 -o /dev/null -w "%{http_code}" \
      -X POST \
      -H "Content-Type: application/x-protobuf" \
      --data-binary "" \
      "${OTLP_URL}${path}" || echo "failed")"
    if [[ "$code" == "200" ]]; then
      status="$code"
      break
    fi
    status="$code"
    sleep 1
  done
  echo "  POST ${OTLP_URL}${path} -> HTTP ${status}"
  if [[ "$status" != "200" ]]; then
    echo "ERROR: OTLP ${label} ingestion failed with HTTP ${status} (expected 200)" >&2
    exit 1
  fi
}

check_otlp_endpoint 3 "Traces" "/v1/traces"
check_otlp_endpoint 4 "Logs" "/v1/logs"
check_otlp_endpoint 5 "Metrics" "/v1/metrics"

echo ""
echo "=========================================================="
echo " SUCCESS: All Aspire Dashboard smoke tests passed!"
echo " Web UI:      $UI_URL"
echo " OTLP Ingest: $OTLP_URL"
echo "=========================================================="
