# Runbook: Shared Agent Memory with `chdb_brain` MCP & On-Demand `rustfs` S3

This runbook covers how to start, configure, and use [`chdb_brain`](../chdb_brain/README.md) (embedded ClickHouse/chDB MCP + HTTP server) and [`rustfs`](../rustfs/README.md) (local S3-compatible object storage) to share agent memory across:

- **`agy-cli` on the Host PC** (`~/.gemini/antigravity-cli`)
- **`agy-cli` inside a guest VM** (`~/.gemini/antigravity-cli`)
- **Other host LLM CLIs** (`claude`, `codex`)

---

## 1. Architecture & Memory Model

```
Host PC: agy-cli (~/.gemini), claude, codex ──┐
                                               │ MCP over HTTP (:8765/mcp)
Guest VM: agy-cli (~/.gemini) ─────────────────┤
                                               ▼
                             ┌───────────────────────────────────┐
                             │  chdb_brain (Host PC, :8765)      │
                             │  - Python + FastMCP + Uvicorn     │
                             │  - In-process chDB (one writer)   │
                             │  - Idle auto-close after 60s      │
                             └─────────────────┬─────────────────┘
                                               │ MergeTree / ReplacingMergeTree
                                               ▼
                             ┌───────────────────────────────────┐
                             │  Local disk: chdb_brain/data/     │
                             │  - brain.memories                 │
                             │  - brain.transcripts              │
                             └─────────────────┬─────────────────┘
                                               │ On-demand Parquet backup/restore
                                               ▼
                             ┌───────────────────────────────────┐
                             │  rustfs (Host PC, :9100 / :9101)  │
                             │  - restart: "no" (0 MiB idle RAM) │
                             │  - Bucket: agent-brain/brains/... │
                             │  - Disk: $RUSTFS_DATA_DIR         │
                             └───────────────────────────────────┘
```

### Key Principles
- **Native local memory stays intact:** `agy-cli` continues using `~/.gemini/antigravity-cli` natively on both Host PC and VM. `chdb_brain` does **not** replace or delete `~/.gemini`.
- **On-demand recall:** Agents query `chdb_brain` via MCP tools only when past/cross-agent context is needed (not on every trivial prompt).
- **One shared database (`brain`), tagged by origin:** Rows carry `source` (`'host'` | `'vm'`), `agent` (`'agy-cli'` | `'claude'` | `'codex'`), and `workspace`.
- **Low RAM footprint:** `chdb_brain` closes its chDB session after `60s` of inactivity (dropping idle RAM to ~60–120 MiB) and caps queries (`max_threads=2`, `max_memory_usage=256 MiB`, container limit `512m`). `rustfs` stays stopped (`0 MiB` RAM) except during backup/restore.

---

## 2. One-Time Setup (Host PC)

### 2.1 Configure `chdb_brain/.env`
```bash
cd chdb_brain
cp .env.example .env
```

Key settings in `chdb_brain/.env`:
```dotenv
CHDB_BRAIN_LISTEN_IP=0.0.0.0
CHDB_BRAIN_CONNECT_IP=127.0.0.1
CHDB_BRAIN_PORT=8765
CHDB_BRAIN_MEM_LIMIT=512m
CHDB_BRAIN_IDLE_CLOSE_SEC=60
CHDB_BRAIN_MAX_THREADS=2
CHDB_BRAIN_MAX_QUERY_MEM_BYTES=268435456

# Local RustFS S3 target (zero cloud S3 fees)
CHDB_BRAIN_S3_ENDPOINT=http://127.0.0.1:9100
CHDB_BRAIN_S3_BUCKET=agent-brain
CHDB_BRAIN_S3_PREFIX=brains/vin
CHDB_BRAIN_S3_ACCESS_KEY=rustfsadmin
CHDB_BRAIN_S3_SECRET_KEY=rustfsadmin
```

### 2.2 Configure `rustfs/.env`
```bash
cd ../rustfs
cp .env.example .env
```

Set `RUSTFS_DATA_DIR` to your preferred local hard disk path (created automatically on first run):
```dotenv
RUSTFS_S3_PORT=9100
RUSTFS_CONSOLE_PORT=9101
RUSTFS_MEM_LIMIT=512m
RUSTFS_DATA_DIR=$HOME/ws/s3/rustfs
RUSTFS_ACCESS_KEY=rustfsadmin
RUSTFS_SECRET_KEY=rustfsadmin
RUSTFS_DEFAULT_BUCKET=agent-brain
```

> **Note:** Keep `RUSTFS_ACCESS_KEY` / `RUSTFS_SECRET_KEY` matching `CHDB_BRAIN_S3_ACCESS_KEY` / `CHDB_BRAIN_S3_SECRET_KEY`.

---

## 3. Starting & Verifying `chdb_brain` (Host PC)

```bash
cd chdb_brain
bash scripts/run_chdb_brain.sh
bash scripts/test_chdb_brain.sh
```

- **MCP URL (Host PC):** `http://127.0.0.1:8765/mcp`
- **Health check (non-waking):** `curl http://127.0.0.1:8765/healthz`
- **Deep health check (runs `SELECT 1`):** `curl "http://127.0.0.1:8765/healthz?deep=true"`
- **Check live memory usage:** `podman stats --no-stream chdb_brain`

---

## 4. Connecting Agents to `chdb_brain` MCP

### 4.1 `agy-cli` on the Host PC (`~/.gemini`)
Add `chdb-brain` to your MCP configuration on the host PC:
```json
{
  "mcpServers": {
    "chdb-brain": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

### 4.2 `agy-cli` inside the Guest VM (`~/.gemini`)
Use the host PC gateway IP (the same host-reachable IP used for OTEL → Grafana/Aspire):
```json
{
  "mcpServers": {
    "chdb-brain": {
      "url": "http://<HOST_IP>:8765/mcp"
    }
  }
}
```

Verify connectivity from inside the VM:
```bash
curl -fsS http://<HOST_IP>:8765/healthz
```

### 4.3 Sharing with `claude` & `codex` on the Host PC
- **Claude Code (`claude`):**
  ```bash
  claude mcp add --transport http chdb-brain http://127.0.0.1:8765/mcp
  ```
- **OpenAI Codex (`codex`)** (`~/.codex/config.toml`):
  ```toml
  [mcp_servers.chdb-brain]
  url = "http://127.0.0.1:8765/mcp"
  ```

---

## 5. Mirroring `~/.gemini` Memory (Host PC & VM)

[`chdb_brain/scripts/sync_gemini.py`](../chdb_brain/scripts/sync_gemini.py) uses Python stdlib only and mirrors `brain/*/.system_generated/logs/transcript.jsonl` plus `history.jsonl` into `brain.transcripts`.

- **Read-only on `~/.gemini`:** Never modifies or deletes local files.
- **Idempotent:** Backed by `ReplacingMergeTree` keyed on `(source, agent, conversation_id, entry_id)`, so re-running safely deduplicates.

### Mirror Host PC `~/.gemini`
```bash
cd chdb_brain
python3 scripts/sync_gemini.py \
  --source host \
  --agent agy-cli \
  --url http://127.0.0.1:8765
```

### Mirror Guest VM `~/.gemini`
Run inside the VM:
```bash
python3 /local-infra/chdb_brain/scripts/sync_gemini.py \
  --source vm \
  --agent agy-cli \
  --url http://<HOST_IP>:8765
```

---

## 6. On-Demand S3 Backup & Restore with RustFS

`rustfs` is configured with `restart: "no"` so it stays stopped (`0 MiB` RAM) until needed. Use [`chdb_brain/scripts/backup_to_rustfs.sh`](../chdb_brain/scripts/backup_to_rustfs.sh) on the host PC to start RustFS, run the native chDB `s3()` Parquet snapshot, and stop RustFS immediately afterward:

```bash
cd chdb_brain

# Backup brain.memories + brain.transcripts -> RustFS Parquet snapshots
bash scripts/backup_to_rustfs.sh

# Restore (append into local MergeTree tables)
bash scripts/backup_to_rustfs.sh --restore

# Restore (truncate local tables first, then import from RustFS)
bash scripts/backup_to_rustfs.sh --restore-replace
```

Snapshots are written to:
- `http://127.0.0.1:9100/agent-brain/brains/vin/memories.parquet`
- `http://127.0.0.1:9100/agent-brain/brains/vin/transcripts.parquet`

To inspect buckets manually in the RustFS Web Console (`http://127.0.0.1:9101`):
```bash
cd rustfs
bash scripts/run_rustfs.sh
# ... browse http://127.0.0.1:9101 ...
podman stop rustfs   # or: docker stop rustfs
```

### 6.1 Host-Side Mirror & chDB SQL Search on RustFS

When `chdb_brain` cannot write to RustFS itself (e.g. it runs in the guest VM, where `127.0.0.1:9100` is not the host), use the host-side helpers. They read settings from `chdb_brain/.env` (falling back to `rustfs/.env`).

```bash
cd chdb_brain
python3 -m venv .venv && .venv/bin/pip install -r requirements-host.txt   # one-time (chdb)

# SQL against the live chdb_brain (stdlib only, via MCP brain_query)
python3 scripts/brain_query.py "SELECT role, count() FROM brain.transcripts FINAL GROUP BY role"

# Snapshot chdb_brain -> RustFS Parquet using local chDB s3()
.venv/bin/python scripts/mirror_to_rustfs.py

# chDB SQL directly on RustFS Parquet (`tx` = transcripts, `mem` = memories)
.venv/bin/python scripts/rustfs_sql.py \
  "SELECT conversation_id, role, substring(content,1,200) FROM tx
   WHERE positionCaseInsensitive(content,'podman') > 0 LIMIT 10"
```

---

## 7. MCP Tools & HTTP Endpoints Reference

| Interface | Name / Route | Purpose |
| :--- | :--- | :--- |
| MCP Tool | `brain_remember` | Store a durable memory tagged with `source`, `agent`, `workspace`, `category`, `tags`. |
| MCP Tool | `brain_search` | Search `brain.memories` (set `include_transcripts=true` to also search mirrored `~/.gemini` transcripts). |
| MCP Tool | `brain_recent` | Fetch recent memories filtered by `source`, `agent`, or `workspace`. |
| MCP Tool | `brain_query` | Run read-only ClickHouse SQL (`SELECT`, `WITH`, `SHOW`, `DESCRIBE`, `EXPLAIN`) including table functions. |
| MCP Tool | `brain_stats` | Counts grouped by `(source, agent)` + S3 config status. |
| MCP Tool | `brain_backup` / `brain_restore` | Trigger S3 Parquet backup/restore while RustFS is running. |
| HTTP | `GET /healthz` | Non-waking health check (`?deep=true` runs `SELECT 1`). |
| HTTP | `POST/GET /api/memories` | Store or search memories via REST. |
| HTTP | `POST /api/ingest/transcripts` | Bulk ingest endpoint used by `sync_gemini.py`. |
| HTTP | `POST /api/backup` / `POST /api/restore` | Trigger S3 Parquet backup/restore (`?replace=true` to truncate before restore). |

---

## 8. Troubleshooting

- **`[FATAL] Server runtime failed: Io error: Permission denied (os error 13)` (Fedora Atomic / rootless Podman):**
  `rustfs/docker-compose.yaml` sets `user: '0:0'` (container root = your host user under rootless Podman) and mounts `'${RUSTFS_DATA_DIR}:/data:Z'` (SELinux relabel). `/data` is only the in-container path; data lands in `RUSTFS_DATA_DIR` on the host (e.g. `/var/home/vin/ws/s3/rustfs`). Ensure that host directory is owned by your user.
- **Port 8765 already in use by `VM:fedora`:**
  A `chdb_brain` running inside the guest VM is port-forwarded to the host. Run only one `chdb_brain` (host is the default). If you keep the VM instance, set `CHDB_BRAIN_S3_ENDPOINT=http://<HOST_IP>:9100` in the VM's `.env`, since `127.0.0.1` inside the VM is not the host.
- **Port conflict on startup:**
  `chdb_brain` uses `:8765`; `rustfs` uses `:9100` (S3) and `:9101` (Console) to avoid clashing with HyperDX (`:9000`) and Langfuse MinIO (`:9090`). Change ports in the respective `.env` files if needed.
- **VM cannot reach `http://<HOST_IP>:8765/healthz`:**
  Verify `CHDB_BRAIN_LISTEN_IP=0.0.0.0` in `chdb_brain/.env` and allow port `8765/tcp` in the host firewall if enabled.
