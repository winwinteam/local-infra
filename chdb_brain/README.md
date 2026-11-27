# chDB Brain (`chdb_brain`)

Single-writer **chDB** analytical memory server exposed over **Streamable HTTP MCP** (`/mcp`) and REST (`/api/*`).

Designed to serve **one shared brain** across:
- **Environments:** host PC (`source="host"`) and guest VMs (`source="vm"`)
- **LLM Agents:** `agy-cli`, `claude` (Claude Code), and `codex` (OpenAI Codex)

All agents read and write the same `brain` database (tagged with `source`, `agent`, `workspace`), while a single container on the host PC owns the sole `chdb.session.Session` (`one writer`).

---

## Quick Start (Run on Host PC)

```bash
cd chdb_brain
cp .env.example .env
bash scripts/run_chdb_brain.sh
bash scripts/test_chdb_brain.sh
```

- **MCP endpoint (Host PC):** `http://127.0.0.1:8765/mcp`
- **MCP endpoint (from VM):** `http://<HOST_IP>:8765/mcp` (same host IP used for OTEL → Grafana)
- **Health check:** `http://127.0.0.1:8765/healthz`

---

## Connecting Agents (Shared Cross-Provider Memory)

### 1. `agy-cli` (Host PC or VM)
Add `chdb-brain` to your MCP server config (`~/.gemini/antigravity-cli/mcp/...` or project MCP config):
```json
{
  "mcpServers": {
    "chdb-brain": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```
*(In the VM, replace `127.0.0.1` with your host PC IP.)*

### 2. Claude Code (`claude`)
```bash
claude mcp add --transport http chdb-brain http://127.0.0.1:8765/mcp
```

### 3. OpenAI Codex (`codex`)
In `~/.codex/config.toml`:
```toml
[mcp_servers.chdb-brain]
url = "http://127.0.0.1:8765/mcp"
```

---

## Exposed MCP Tools

| Tool | Purpose |
| :--- | :--- |
| `brain_remember` | Store a durable memory tagged with `source`, `agent`, `workspace`, `category`, `tags`. |
| `brain_search` | Search shared memories (and optionally mirrored transcripts via `include_transcripts=true`). |
| `brain_recent` | List the most recent memories (filterable by `source`, `agent`, `workspace`). |
| `brain_query` | Run read-only ClickHouse SQL (`SELECT`, `WITH`, `SHOW`, `DESCRIBE`, `EXPLAIN`) — supports chDB table functions (`s3()`, `url()`, `postgresql()`, etc.). |
| `brain_stats` | View memory and transcript counts grouped by `(source, agent)`. |
| `brain_backup` | Export `memories` and `transcripts` as Parquet snapshots to local RustFS/S3. |
| `brain_restore` | Restore `memories` and `transcripts` from Parquet snapshots in RustFS/S3. |

---

## Local S3 Backup & Restore (RustFS — Zero S3 Fees, On-Demand)

RustFS is configured with `restart: "no"` so it stays stopped (`0 MiB` RAM) until you run a backup or restore:

1. One-time setup: copy `rustfs/.env.example` → `rustfs/.env` and add `CHDB_BRAIN_S3_*` from `chdb_brain/.env.example` into `chdb_brain/.env`.
2. Run on-demand backup or restore (automatically starts RustFS, syncs Parquet snapshots to `agent-brain/brains/vin/`, and stops RustFS immediately after):
   ```bash
   cd chdb_brain
   bash scripts/backup_to_rustfs.sh                   # backup
   bash scripts/backup_to_rustfs.sh --restore         # append restore
   bash scripts/backup_to_rustfs.sh --restore-replace # truncate & restore
   ```

---

## Mirroring `~/.gemini` Memory (Host PC & VM)

`scripts/sync_gemini.py` uses Python stdlib only and ingests `transcript.jsonl` + `history.jsonl` idempotently into `brain.transcripts` (`ReplacingMergeTree` keyed by `(source, agent, conversation_id, entry_id)`):

```bash
# On Host PC:
python3 scripts/sync_gemini.py --source host --url http://127.0.0.1:8765

# Inside VM:
python3 scripts/sync_gemini.py --source vm --url http://<HOST_IP>:8765
```
