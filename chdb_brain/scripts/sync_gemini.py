#!/usr/bin/env python3
"""Mirror ~/.gemini/antigravity-cli transcripts & history into chDB Brain.

Zero third-party dependencies (Python 3 stdlib only), so it can run directly on
both the host PC and guest VMs.

Examples:
    # On host PC:
    python3 scripts/sync_gemini.py --source host --url http://127.0.0.1:8765

    # Inside VM (pointing to host PC gateway IP):
    python3 scripts/sync_gemini.py --source vm --url http://<HOST_IP>:8765
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any


def _extract_text(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for key in ("text", "content", "message", "prompt", "output"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, list):
        parts = [_extract_text(item) for item in obj]
        return "\n".join(p for p in parts if p)
    return str(obj)


def _collect_brain_transcripts(gemini_dir: Path, source: str, agent: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    brain_dir = gemini_dir / "brain"
    if not brain_dir.is_dir():
        return rows

    for transcript_path in sorted(brain_dir.glob("*/.system_generated/logs/transcript.jsonl")):
        conv_id = transcript_path.parents[2].name
        try:
            with transcript_path.open("r", encoding="utf-8", errors="replace") as f:
                for idx, line in enumerate(f):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    content = _extract_text(rec).strip()
                    if not content:
                        continue
                    role = (
                        str(rec.get("role") or rec.get("type") or "entry")
                        if isinstance(rec, dict)
                        else "entry"
                    )
                    ts = (
                        str(rec.get("timestamp") or rec.get("created_at") or "")
                        if isinstance(rec, dict)
                        else ""
                    )
                    workspace = str(rec.get("workspace") or "") if isinstance(rec, dict) else ""
                    rows.append(
                        {
                            "source": source,
                            "agent": agent,
                            "conversation_id": conv_id,
                            "entry_id": f"line-{idx}",
                            "role": role,
                            "workspace": workspace,
                            "content": content[:16000],
                            "timestamp": ts,
                        }
                    )
        except OSError as exc:
            print(f"WARN: skipped {transcript_path}: {exc}", file=sys.stderr)
    return rows


def _collect_history(gemini_dir: Path, source: str, agent: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    history_path = gemini_dir / "history.jsonl"
    if not history_path.is_file():
        return rows

    with history_path.open("r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                rec = {"text": line}
            content = _extract_text(rec).strip()
            if not content:
                continue
            digest = hashlib.sha1(f"{idx}:{content}".encode(), usedforsecurity=False).hexdigest()[
                :16
            ]
            ts = str(rec.get("timestamp") or "") if isinstance(rec, dict) else ""
            rows.append(
                {
                    "source": source,
                    "agent": agent,
                    "conversation_id": "history",
                    "entry_id": f"hist-{idx}-{digest}",
                    "role": "user",
                    "workspace": str(rec.get("cwd") or rec.get("workspace") or "")
                    if isinstance(rec, dict)
                    else "",
                    "content": content[:16000],
                    "timestamp": ts,
                }
            )
    return rows


def _post_batch(url: str, rows: list[dict[str, Any]]) -> int:
    payload = json.dumps({"rows": rows}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/ingest/transcripts",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        return int(body.get("ingested", 0))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mirror ~/.gemini/antigravity-cli into chDB Brain."
    )
    parser.add_argument("--source", required=True, help="Origin tag, e.g. 'host' or 'vm'")
    parser.add_argument("--agent", default="agy-cli", help="Agent tag (default: agy-cli)")
    parser.add_argument(
        "--gemini-dir",
        default=str(Path.home() / ".gemini" / "antigravity-cli"),
        help="Path to ~/.gemini/antigravity-cli",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8765", help="chDB Brain base URL")
    parser.add_argument("--batch-size", type=int, default=100, help="Rows per POST batch")
    args = parser.parse_args()

    gemini_dir = Path(args.gemini_dir).expanduser()
    if not gemini_dir.is_dir():
        print(f"ERROR: directory not found: {gemini_dir}", file=sys.stderr)
        sys.exit(1)

    rows = _collect_brain_transcripts(gemini_dir, args.source, args.agent)
    rows.extend(_collect_history(gemini_dir, args.source, args.agent))
    print(
        f"Collected {len(rows)} entries from {gemini_dir} "
        f"(source={args.source}, agent={args.agent})"
    )

    total = 0
    for i in range(0, len(rows), args.batch_size):
        batch = rows[i : i + args.batch_size]
        total += _post_batch(args.url, batch)
    print(f"Ingested {total} rows into {args.url}")


if __name__ == "__main__":
    main()
