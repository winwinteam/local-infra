#!/usr/bin/env python3
"""Run read-only chDB SQL against the live chdb_brain via its `brain_query` MCP tool.

Zero third-party dependencies (Python 3 stdlib only).

Examples:
    python3 scripts/brain_query.py "SELECT role, count() FROM brain.transcripts FINAL GROUP BY role"
    python3 scripts/brain_query.py --tool brain_stats
    python3 scripts/brain_query.py --url http://<HOST_IP>:8765 "SHOW TABLES FROM brain"
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _brain_env import load_config  # noqa: E402


def call_tool(base_url: str, tool: str, arguments: dict[str, Any], timeout: float = 120) -> str:
    """Call an MCP tool over Streamable HTTP and return its text content."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    ).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/mcp",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read().decode("utf-8")

    messages = [line[6:] for line in payload.splitlines() if line.startswith("data: ")] or [payload]
    for raw in messages:
        msg = json.loads(raw)
        if "error" in msg:
            raise RuntimeError(json.dumps(msg["error"]))
        result = msg.get("result", {})
        text = "\n".join(c.get("text", "") for c in result.get("content", []))
        if result.get("isError"):
            raise RuntimeError(text)
        return text
    raise RuntimeError("Empty MCP response")


def brain_query(base_url: str, sql: str, output_format: str = "JSONEachRow") -> str:
    return call_tool(base_url, "brain_query", {"sql": sql, "output_format": output_format})


def main() -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("sql", nargs="?", help="Read-only SQL (SELECT/WITH/SHOW/DESCRIBE/EXPLAIN)")
    parser.add_argument(
        "--format", default="PrettyCompactMonoBlock", help="ClickHouse output format"
    )
    parser.add_argument(
        "--url", default=cfg["brain_url"], help=f"chdb_brain base URL (default: {cfg['brain_url']})"
    )
    parser.add_argument(
        "--tool", help="Call another MCP tool with no arguments instead (e.g. brain_stats)"
    )
    args = parser.parse_args()

    try:
        if args.tool:
            print(call_tool(args.url, args.tool, {}))
        elif args.sql:
            print(brain_query(args.url, args.sql, args.format))
        else:
            parser.error("provide SQL or --tool")
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
