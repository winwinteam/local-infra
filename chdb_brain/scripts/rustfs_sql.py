#!/usr/bin/env python3
"""Run chDB SQL directly against the chdb_brain Parquet snapshots in RustFS.

The placeholders `tx` and `mem` are replaced with s3() table functions for
transcripts.parquet and memories.parquet (endpoint/bucket/prefix/credentials
come from chdb_brain/.env, falling back to rustfs/.env).

Requires: chdb (see requirements-host.txt), RustFS running.

Examples:
    .venv/bin/python scripts/rustfs_sql.py "SELECT role, count() FROM tx GROUP BY role"
    .venv/bin/python scripts/rustfs_sql.py \\
      "SELECT conversation_id, substring(content,1,200) FROM tx
       WHERE positionCaseInsensitive(content,'podman') > 0 LIMIT 10"
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _brain_env import load_config, s3_table  # noqa: E402

try:
    import chdb
except ImportError:
    sys.exit(
        "chdb not installed. Run: "
        "python3 -m venv .venv && .venv/bin/pip install -r requirements-host.txt"
    )


def expand(sql: str, cfg: dict[str, str]) -> str:
    sql = re.sub(r"\btx\b", lambda _: s3_table(cfg, "transcripts"), sql)
    return re.sub(r"\bmem\b", lambda _: s3_table(cfg, "memories"), sql)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("sql", help="chDB SQL using `tx` / `mem` placeholders")
    parser.add_argument(
        "--format", default="PrettyCompactMonoBlock", help="ClickHouse output format"
    )
    args = parser.parse_args()

    try:
        print(chdb.query(expand(args.sql, load_config()), args.format))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
