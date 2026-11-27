# RustFS (`rustfs`)

Lightweight, self-hosted, S3-compatible object storage (written in Rust) for local development—zero cloud S3 fees.

Designed to act as `Your bucket` for [`chdb_brain`](../chdb_brain/README.md) (`s3://agent-brain/brains/<user>/`), running on the host PC with `network_mode: host`.

## Ports
- **S3 API:** `http://127.0.0.1:9100` (avoids `:9000` used by HyperDX ClickHouse and `:9090` used by Langfuse MinIO)
- **Web Console:** `http://127.0.0.1:9101`

## Quick Start (Host PC)

```bash
cd rustfs
cp .env.example .env
# Edit RUSTFS_ACCESS_KEY / RUSTFS_SECRET_KEY in .env if desired
bash scripts/run_rustfs.sh
bash scripts/test_rustfs.sh
```

`run_rustfs.sh` automatically creates the default bucket (`agent-brain`) so `chdb_brain` can immediately export/restore Parquet snapshots via chDB's native `s3()` table function.

> **On-demand mode (`restart: "no"`):** RustFS does not auto-start on boot. For zero idle RAM, use `bash ../chdb_brain/scripts/backup_to_rustfs.sh`—it starts RustFS, runs the backup/restore, and stops RustFS automatically on exit.
