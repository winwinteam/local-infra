#!/usr/bin/env bash
# Smoke test for chDB Brain MCP + HTTP server.
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

BASE_URL="http://${CHDB_BRAIN_CONNECT_IP}:${CHDB_BRAIN_PORT}"
TOKEN="smoke-$(date +%s)"

echo "1) Checking /healthz ..."
HEALTH="$(curl -fsS "${BASE_URL}/healthz")"
echo "   ${HEALTH}"

echo "2) Storing a test memory via POST /api/memories ..."
CREATED="$(curl -fsS -X POST "${BASE_URL}/api/memories" \
  -H "Content-Type: application/json" \
  -d "{\"title\":\"smoke test\",\"content\":\"chdb-brain verified ${TOKEN}\",\"source\":\"host\",\"agent\":\"smoke-test\",\"workspace\":\"local-infra\",\"category\":\"note\",\"tags\":[\"smoke\"]}")"
echo "   ${CREATED}"

echo "3) Searching test memory via GET /api/memories?q=${TOKEN} ..."
FOUND="$(curl -fsS "${BASE_URL}/api/memories?q=${TOKEN}")"
echo "${FOUND}" | grep -q "${TOKEN}" || { echo "ERROR: Could not find inserted memory" >&2; exit 1; }
echo "   Found memory OK."

echo "4) Testing MCP initialize handshake on POST /mcp ..."
MCP_RESP="$(curl -fsS -X POST "${BASE_URL}/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke-test","version":"0.1.0"}}}')"
echo "${MCP_RESP}" | grep -q "chdb-brain" || { echo "ERROR: Unexpected MCP response: ${MCP_RESP}" >&2; exit 1; }
echo "   MCP initialize OK."

echo "ALL SMOKE TESTS PASSED!"
