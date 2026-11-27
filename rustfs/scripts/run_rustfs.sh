#!/usr/bin/env bash
# Start local RustFS S3-compatible object storage (Podman-first with Docker fallback).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SERVICE_DIR/.." && pwd)"
cd "$SERVICE_DIR"

# shellcheck source=../../scripts/compose_hash.sh
source "$REPO_ROOT/scripts/compose_hash.sh"

ENV_FILE="$SERVICE_DIR/.env"
[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE (copy .env.example)" >&2; exit 1; }
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

: "${RUSTFS_IMAGE_TAG:?RUSTFS_IMAGE_TAG must be set in .env}"
: "${RUSTFS_LISTEN_IP:?RUSTFS_LISTEN_IP must be set in .env}"
: "${RUSTFS_CONNECT_IP:?RUSTFS_CONNECT_IP must be set in .env}"
: "${RUSTFS_S3_PORT:?RUSTFS_S3_PORT must be set in .env}"
: "${RUSTFS_CONSOLE_PORT:?RUSTFS_CONSOLE_PORT must be set in .env}"
: "${RUSTFS_CONSOLE_ENABLE:?RUSTFS_CONSOLE_ENABLE must be set in .env}"
: "${RUSTFS_MEM_LIMIT:?RUSTFS_MEM_LIMIT must be set in .env}"
: "${RUSTFS_DATA_DIR:?RUSTFS_DATA_DIR must be set in .env}"
: "${RUSTFS_ACCESS_KEY:?RUSTFS_ACCESS_KEY must be set in .env}"
: "${RUSTFS_SECRET_KEY:?RUSTFS_SECRET_KEY must be set in .env}"
: "${RUSTFS_DEFAULT_BUCKET:?RUSTFS_DEFAULT_BUCKET must be set in .env}"
: "${TZ:?TZ must be set in .env}"

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

mkdir -p "$RUSTFS_DATA_DIR"

S3_URL="http://${RUSTFS_CONNECT_IP}:${RUSTFS_S3_PORT}"
CONSOLE_URL="http://${RUSTFS_CONNECT_IP}:${RUSTFS_CONSOLE_PORT}"

echo "=== Starting RustFS from local-infra (${COMPOSE_CMD[*]}) ==="

if command -v ss >/dev/null 2>&1; then
  CONFLICTS=""
  for port in "$RUSTFS_S3_PORT" "$RUSTFS_CONSOLE_PORT"; do
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      case "$line" in *rustfs*) continue ;; esac
      CONFLICTS+="  port ${port}: ${line}"$'\n'
    done < <(ss -ltnpH "sport = :${port}" 2>/dev/null || true)
  done
  if [ -n "$CONFLICTS" ]; then
    echo "ERROR: Required port(s) already in use by another process:" >&2
    printf '%s' "$CONFLICTS" >&2
    exit 1
  fi
fi

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

echo "Waiting for RustFS S3 endpoint on ${S3_URL} ..."
READY=0
for i in {1..30}; do
  STATUS="$(curl -s --max-time 3 --connect-timeout 2 -o /dev/null -w "%{http_code}" "${S3_URL}/health" || true)"
  STATUS="${STATUS:-000}"
  # Any non-000 HTTP response (200/403/404) confirms RustFS HTTP listener is up
  if [ "$STATUS" != "000" ]; then
    echo "RustFS S3 listener is ready! (HTTP ${STATUS})"
    READY=1
    break
  fi
  sleep 2
  echo "  ... waiting for RustFS ($i/30, last status: ${STATUS})"
done

if [ "$READY" -ne 1 ]; then
  echo "ERROR: RustFS failed to become ready within 60 seconds." >&2
  "$CONTAINER_CLI" logs --tail 50 rustfs 2>/dev/null || true
  exit 1
fi

echo "Ensuring default bucket '${RUSTFS_DEFAULT_BUCKET}' exists ..."
python3 "$SCRIPT_DIR/ensure_bucket.py" \
  --endpoint "$S3_URL" \
  --bucket "$RUSTFS_DEFAULT_BUCKET" \
  --access-key "$RUSTFS_ACCESS_KEY" \
  --secret-key "$RUSTFS_SECRET_KEY"

echo ""
echo "=========================================================="
echo " RustFS Running (local-infra)"
echo "=========================================================="
echo " - Runtime:   ${COMPOSE_CMD[*]}"
echo " - S3 API:    ${S3_URL}"
echo " - Console:   ${CONSOLE_URL}"
echo " - Bucket:    ${RUSTFS_DEFAULT_BUCKET}"
echo " - Data dir:  ${RUSTFS_DATA_DIR}"
echo "=========================================================="
echo "Run smoke tests: bash scripts/test_rustfs.sh"
echo "=========================================================="
