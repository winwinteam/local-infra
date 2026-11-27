#!/usr/bin/env bash
# Start the chDB Brain MCP + HTTP server (Podman-first with Docker fallback).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SERVICE_DIR/.." && pwd)"
cd "$SERVICE_DIR"

# Reuse deterministic stack hash calculation from repo-wide helper
# shellcheck source=../../scripts/compose_hash.sh
source "$REPO_ROOT/scripts/compose_hash.sh"

# SSOT: local .env in chdb_brain/ directory
ENV_FILE="$SERVICE_DIR/.env"
[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE (copy .env.example)" >&2; exit 1; }
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

: "${CHDB_BRAIN_LISTEN_IP:?CHDB_BRAIN_LISTEN_IP must be set in .env}"
: "${CHDB_BRAIN_CONNECT_IP:?CHDB_BRAIN_CONNECT_IP must be set in .env}"
: "${CHDB_BRAIN_PORT:?CHDB_BRAIN_PORT must be set in .env}"
: "${CHDB_BRAIN_DB_NAME:?CHDB_BRAIN_DB_NAME must be set in .env}"
: "${CHDB_BRAIN_DATA_DIR:?CHDB_BRAIN_DATA_DIR must be set in .env}"
: "${CHDB_BRAIN_MEM_LIMIT:?CHDB_BRAIN_MEM_LIMIT must be set in .env}"
: "${TZ:?TZ must be set in .env}"

# Detect Compose runtime: prefer Podman, fall back to Docker
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

mkdir -p "$SERVICE_DIR/data"

BASE_URL="http://${CHDB_BRAIN_CONNECT_IP}:${CHDB_BRAIN_PORT}"
MCP_URL="${BASE_URL}/mcp"

echo "=== Starting chDB Brain from local-infra (${COMPOSE_CMD[*]}) ==="

# Preflight port check (host networking)
if command -v ss >/dev/null 2>&1; then
  CONFLICTS=""
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    case "$line" in *python*|*uvicorn*) continue ;; esac
    CONFLICTS+="  port ${CHDB_BRAIN_PORT}: ${line}"$'\n'
  done < <(ss -ltnpH "sport = :${CHDB_BRAIN_PORT}" 2>/dev/null || true)
  if [ -n "$CONFLICTS" ]; then
    echo "ERROR: Required port ${CHDB_BRAIN_PORT} already in use by another process:" >&2
    printf '%s' "$CONFLICTS" >&2
    exit 1
  fi
fi

# Include app/ source files in hash so code edits trigger image rebuild
STACK_HASH="$(compute_stack_hash "$SERVICE_DIR")"
APP_HASH="$(find "$SERVICE_DIR/app" -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}')"
CURRENT_HASH="$(printf '%s\n%s\n' "$STACK_HASH" "$APP_HASH" | sha256sum | awk '{print $1}')"

STATE_FILE="$SERVICE_DIR/.compose.sha256"
PREVIOUS_HASH=""
if [ -f "$STATE_FILE" ]; then
  PREVIOUS_HASH="$(cat "$STATE_FILE" 2>/dev/null || true)"
fi

if [ -z "$PREVIOUS_HASH" ]; then
  echo "  [hash-check] No previous state found (${CURRENT_HASH:0:12}...). Running initial '${COMPOSE_CMD[*]} up -d --build'..."
  "${COMPOSE_CMD[@]}" up -d --build "$@"
elif [ "$CURRENT_HASH" != "$PREVIOUS_HASH" ]; then
  echo "  [hash-check] Config/code change detected (${PREVIOUS_HASH:0:12}... -> ${CURRENT_HASH:0:12}...). Running with '--build --force-recreate'..."
  "${COMPOSE_CMD[@]}" up -d --build --force-recreate "$@"
else
  echo "  [hash-check] Config/code unchanged (${CURRENT_HASH:0:12}...). Skipping rebuild..."
  "${COMPOSE_CMD[@]}" up -d "$@"
fi
echo "$CURRENT_HASH" > "$STATE_FILE"

echo "Waiting for chDB Brain healthz on ${BASE_URL}/healthz ..."
READY=0
for i in {1..30}; do
  STATUS="$(curl -s --max-time 3 --connect-timeout 2 -o /dev/null -w "%{http_code}" "${BASE_URL}/healthz" || true)"
  STATUS="${STATUS:-000}"
  if [ "$STATUS" = "200" ]; then
    echo "chDB Brain is ready! (HTTP 200)"
    READY=1
    break
  fi
  sleep 2
  echo "  ... waiting for chDB Brain ($i/30, last status: ${STATUS})"
done

if [ "$READY" -ne 1 ]; then
  echo "" >&2
  echo "ERROR: chDB Brain failed to become ready within 60 seconds." >&2
  "$CONTAINER_CLI" logs --tail 50 chdb_brain 2>/dev/null || true
  exit 1
fi

echo ""
echo "=========================================================="
echo " chDB Brain Running (local-infra)"
echo "=========================================================="
echo " - Runtime:   ${COMPOSE_CMD[*]}"
echo " - MCP URL:   ${MCP_URL}"
echo " - Health:    ${BASE_URL}/healthz"
echo " - Data dir:  ${SERVICE_DIR}/data"
echo "=========================================================="
echo "Run smoke tests: bash scripts/test_chdb_brain.sh"
echo "=========================================================="
