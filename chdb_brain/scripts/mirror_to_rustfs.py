#!/usr/bin/env python3
"""Snapshot chdb_brain tables to RustFS Parquet using host-side chDB.

Pulls rows from the live chdb_brain over MCP (`brain_query`), then writes them to
RustFS with local chDB's `s3()` table function. Unlike `backup_to_rustfs.sh`
(which asks the server to write to S3), this works even when chdb_brain runs in a
VM that cannot reach the host's RustFS on 127.0.0.1.

Requires: chdb (see requirements-host.txt), RustFS running.

Examples:
    .venv/bin/python scripts/mirror_to_rustfs.py
    .venv/bin/python scripts/mirror_to_rustfs.py --tables transcripts
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _brain_env import load_config, s3_table, sql_quote  # noqa: E402
from brain_query import brain_query  # noqa: E402

try:
    import chdb
except ImportError:
    sys.exit(
        "chdb not installed. Run: "
        "python3 -m venv .venv && .venv/bin/pip install -r requirements-host.txt"
    )

TABLE_SQL = {
    "transcripts": "SELECT * FROM brain.transcripts FINAL",
    "memories": "SELECT * FROM brain.memories",
}


def mirror_table(cfg: dict[str, str], name: str) -> int:
    schema = brain_query(cfg["brain_url"], f"DESCRIBE brain.{name}", "JSONEachRow")
    columns = ", ".join(
        f"`{col['name']}` {col['type']}"
        for col in (json.loads(line) for line in schema.splitlines() if line.strip())
    )
    rows = brain_query(cfg["brain_url"], TABLE_SQL[name], "JSONEachRow")

    fd, path = tempfile.mkstemp(suffix=f"-{name}.jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(rows)
        target = s3_table(cfg, name)
        chdb.query(
            f"INSERT INTO FUNCTION {target} SETTINGS s3_truncate_on_insert = 1 "
            f"SELECT * FROM file({sql_quote(path)}, 'JSONEachRow', {sql_quote(columns)})"
        )
        return int(chdb.query(f"SELECT count() FROM {target}", "CSV").bytes().decode().strip())
    finally:
        os.unlink(path)


def main() -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default=cfg["brain_url"], help="chdb_brain base URL")
    parser.add_argument("--tables", nargs="+", choices=sorted(TABLE_SQL), default=sorted(TABLE_SQL))
    args = parser.parse_args()
    cfg["brain_url"] = args.url.rstrip("/")

    dest = f"{cfg['s3_endpoint']}/{cfg['s3_bucket']}/{cfg['s3_prefix']}/"
    print(f"Source: {cfg['brain_url']}  ->  {dest}")
    for name in args.tables:
        print(f"  {name}: {mirror_table(cfg, name)} rows in RustFS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
