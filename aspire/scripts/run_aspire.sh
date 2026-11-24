#!/usr/bin/env bash
# Start the standalone Aspire Dashboard OTel UI stack (Podman-first with Docker fallback).
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

: "${ASPIRE_LISTEN_IP:?ASPIRE_LISTEN_IP must be set in .env}"
: "${ASPIRE_CONNECT_IP:?ASPIRE_CONNECT_IP must be set in .env}"
: "${ASPIRE_UI_PORT:?ASPIRE_UI_PORT must be set in .env}"
: "${ASPIRE_OTLP_GRPC_PORT:?ASPIRE_OTLP_GRPC_PORT must be set in .env}"
: "${ASPIRE_OTLP_HTTP_PORT:?ASPIRE_OTLP_HTTP_PORT must be set in .env}"
: "${ASPIRE_MEM_LIMIT:?ASPIRE_MEM_LIMIT must be set in .env}"
: "${TZ:?TZ must be set in .env}"

# Auto-detect Compose CLI: podman compose -> podman-compose -> docker compose
COMPOSE_CMD=()
CONTAINER_CLI=""
if command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(podman compose)
  CONTAINER_CLI="podman"
elif command -v podman-compose >/dev/null 2>&1; then
  COMPOSE_CMD=(podman-compose)
  CONTAINER_CLI="podman"
elif command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(docker compose)
  CONTAINER_CLI="docker"
else
  echo "ERROR: No supported Compose CLI found (tried: 'podman compose', 'podman-compose', 'docker compose')." >&2
  exit 1
fi

UI_URL="http://${ASPIRE_CONNECT_IP}:${ASPIRE_UI_PORT}"
OTLP_HTTP_URL="http://${ASPIRE_CONNECT_IP}:${ASPIRE_OTLP_HTTP_PORT}"
OTLP_GRPC_URL="http://${ASPIRE_CONNECT_IP}:${ASPIRE_OTLP_GRPC_PORT}"

echo "=== Starting Aspire Dashboard from local-infra (${COMPOSE_CMD[*]}) ==="

STATE_FILE="$SERVICE_DIR/.compose.sha256"
CURRENT_HASH="$(compute_stack_hash "$SERVICE_DIR")"
PREVIOUS_HASH=""
if [ -f "$STATE_FILE" ]; then
  PREVIOUS_HASH="$(cat "$STATE_FILE" 2>/dev/null || true)"
fi

if [ -z "$PREVIOUS_HASH" ]; then
  echo "  [hash-check] No previous state found (${CURRENT_HASH:0:12}...). Running initial '${COMPOSE_CMD[*]} up -d'..."
  "${COMPOSE_CMD[@]}" up -d "$@"
elif [ "$CURRENT_HASH" != "$PREVIOUS_HASH" ]; then
  echo "  [hash-check] Config/.env change detected (${PREVIOUS_HASH:0:12}... -> ${CURRENT_HASH:0:12}...). Running with '--force-recreate'..."
  "${COMPOSE_CMD[@]}" up -d --force-recreate "$@"
else
  echo "  [hash-check] Config/.env unchanged (${CURRENT_HASH:0:12}...). Skipping '--force-recreate'..."
  "${COMPOSE_CMD[@]}" up -d "$@"
fi
echo "$CURRENT_HASH" > "$STATE_FILE"

echo "Waiting for Aspire Web UI on ${UI_URL} ..."
READY=0
for i in {1..30}; do
  UI_STATUS="$(curl -sL --max-time 3 --connect-timeout 2 -o /dev/null -w "%{http_code}" "${UI_URL}" || echo "000")"
  if [ "$UI_STATUS" = "200" ]; then
    echo "Aspire Web UI is ready! (HTTP 200)"
    READY=1
    break
  fi
  sleep 2
  echo "  ... waiting for Aspire Web UI ($i/30, last status: ${UI_STATUS})"
done

if [ "$READY" -ne 1 ]; then
  echo "" >&2
  echo "ERROR: Aspire Web UI failed to become ready within 60 seconds." >&2
  echo "Hint: Ensure ports ${ASPIRE_UI_PORT}, ${ASPIRE_OTLP_GRPC_PORT}, and ${ASPIRE_OTLP_HTTP_PORT} are not in use (e.g. stop HyperDX first)." >&2
  "$CONTAINER_CLI" logs --tail 50 aspire 2>/dev/null || true
  exit 1
fi

echo "Waiting for Aspire OTLP HTTP receiver on ${OTLP_HTTP_URL}/v1/traces ..."
OTLP_READY=0
for i in {1..30}; do
  OTLP_STATUS="$(curl -s --max-time 3 --connect-timeout 2 -o /dev/null -w "%{http_code}" \
    -X POST \
    -H "Content-Type: application/x-protobuf" \
    --data-binary "" \
    "${OTLP_HTTP_URL}/v1/traces" || echo "000")"
  if [ "$OTLP_STATUS" = "200" ]; then
    echo "Aspire OTLP HTTP receiver is ready! (HTTP 200)"
    OTLP_READY=1
    break
  fi
  sleep 2
  echo "  ... waiting for Aspire OTLP HTTP receiver ($i/30, last status: ${OTLP_STATUS})"
done

if [ "$OTLP_READY" -ne 1 ]; then
  echo "" >&2
  echo "ERROR: Aspire OTLP HTTP receiver failed to become ready within 60 seconds." >&2
  "$CONTAINER_CLI" logs --tail 50 aspire 2>/dev/null || true
  exit 1
fi

echo ""
echo "=========================================================="
echo " Aspire Dashboard Running (local-infra)"
echo "=========================================================="
echo " - Runtime:   ${COMPOSE_CMD[*]}"
echo " - Web UI:    ${UI_URL}"
echo " - OTLP HTTP: ${OTLP_HTTP_URL} (POST /v1/traces, /v1/logs, /v1/metrics)"
echo " - OTLP gRPC: ${OTLP_GRPC_URL}"
echo "=========================================================="
echo "Client integration settings for consuming repositories:"
echo "  OTEL_EXPORTER_OTLP_ENDPOINT=${OTLP_HTTP_URL}"
echo "  OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf"
echo "  OTEL_EXPORTER_OTLP_HEADERS="
echo "=========================================================="
echo "Run smoke tests: bash scripts/test_aspire.sh"
echo "=========================================================="
