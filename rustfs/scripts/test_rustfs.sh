#!/usr/bin/env bash
# Smoke test for local RustFS S3-compatible object storage.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$SERVICE_DIR"

ENV_FILE="$SERVICE_DIR/.env"
[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE (copy .env.example)" >&2; exit 1; }
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

S3_URL="http://${RUSTFS_CONNECT_IP}:${RUSTFS_S3_PORT}"
CONSOLE_URL="http://${RUSTFS_CONNECT_IP}:${RUSTFS_CONSOLE_PORT}"

echo "1) Checking RustFS S3 listener on ${S3_URL} ..."
S3_STATUS="$(curl -s --max-time 3 -o /dev/null -w "%{http_code}" "${S3_URL}" || true)"
[ "${S3_STATUS:-000}" != "000" ] || { echo "ERROR: S3 endpoint unreachable" >&2; exit 1; }
echo "   S3 listener responded (HTTP ${S3_STATUS})."

echo "2) Checking RustFS Console on ${CONSOLE_URL} ..."
CON_STATUS="$(curl -sL --max-time 3 -o /dev/null -w "%{http_code}" "${CONSOLE_URL}" || true)"
[ "${CON_STATUS:-000}" != "000" ] || { echo "ERROR: Console endpoint unreachable" >&2; exit 1; }
echo "   Console responded (HTTP ${CON_STATUS})."

echo "3) Verifying default bucket '${RUSTFS_DEFAULT_BUCKET}' ..."
python3 "$SCRIPT_DIR/ensure_bucket.py" \
  --endpoint "$S3_URL" \
  --bucket "$RUSTFS_DEFAULT_BUCKET" \
  --access-key "$RUSTFS_ACCESS_KEY" \
  --secret-key "$RUSTFS_SECRET_KEY"

echo "ALL RUSTFS SMOKE TESTS PASSED!"
