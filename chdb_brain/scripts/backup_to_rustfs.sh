#!/usr/bin/env bash
# On-demand backup/restore between chdb_brain and local RustFS.
# Starts RustFS only if stopped, runs the Parquet backup/restore via chdb_brain,
# and stops RustFS immediately afterward so it uses 0 MiB RAM when idle.
#
# Usage:
#   bash scripts/backup_to_rustfs.sh                   # backup to RustFS
#   bash scripts/backup_to_rustfs.sh --restore         # append-restore from RustFS
#   bash scripts/backup_to_rustfs.sh --restore-replace # truncate & restore from RustFS
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SERVICE_DIR/.." && pwd)"
RUSTFS_DIR="$REPO_ROOT/rustfs"

ENV_FILE="$SERVICE_DIR/.env"
[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE (copy .env.example)" >&2; exit 1; }
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

BASE_URL="http://${CHDB_BRAIN_CONNECT_IP}:${CHDB_BRAIN_PORT}"
MODE="backup"
if [ "${1:-}" = "--restore" ]; then
  MODE="restore"
elif [ "${1:-}" = "--restore-replace" ]; then
  MODE="restore-replace"
elif [ -n "${1:-}" ]; then
  echo "Usage: $0 [--restore | --restore-replace]" >&2
  exit 1
fi

if command -v podman >/dev/null 2>&1; then
  CONTAINER_CLI="podman"
elif command -v docker >/dev/null 2>&1; then
  CONTAINER_CLI="docker"
else
  echo "ERROR: Neither podman nor docker found." >&2
  exit 1
fi

WAS_RUNNING=0
if [ "$("$CONTAINER_CLI" inspect -f '{{.State.Running}}' rustfs 2>/dev/null || echo false)" = "true" ]; then
  WAS_RUNNING=1
fi

cleanup() {
  if [ "$WAS_RUNNING" -eq 0 ]; then
    echo "Stopping on-demand RustFS container (0 MiB idle RAM) ..."
    "$CONTAINER_CLI" stop rustfs >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [ "$WAS_RUNNING" -eq 0 ]; then
  echo "Starting RustFS on demand ..."
  bash "$RUSTFS_DIR/scripts/run_rustfs.sh"
fi

case "$MODE" in
  backup)
    echo "Running chdb_brain backup -> RustFS ..."
    curl -fsS -X POST "${BASE_URL}/api/backup"
    echo ""
    ;;
  restore)
    echo "Running chdb_brain restore <- RustFS (append) ..."
    curl -fsS -X POST "${BASE_URL}/api/restore"
    echo ""
    ;;
  restore-replace)
    echo "Running chdb_brain restore <- RustFS (replace) ..."
    curl -fsS -X POST "${BASE_URL}/api/restore?replace=true"
    echo ""
    ;;
esac
