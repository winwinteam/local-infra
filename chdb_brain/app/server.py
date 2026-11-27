"""Single-writer chDB Brain MCP + HTTP server.

Serves a shared ClickHouse/chDB analytical memory store to multiple agents
(agy-cli, claude, codex) across both the host PC and guest VMs over Streamable
HTTP MCP (`/mcp`) and REST (`/healthz`, `/api/memories`, `/api/ingest/transcripts`).
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import gc
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import chdb
import chdb.session as chs
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

LISTEN_IP = os.environ["CHDB_BRAIN_LISTEN_IP"]
PORT = int(os.environ["CHDB_BRAIN_PORT"])
DB_NAME = os.environ["CHDB_BRAIN_DB_NAME"]
DATA_DIR = os.environ["CHDB_BRAIN_DATA_DIR"]

IDLE_CLOSE_SEC = int(os.environ.get("CHDB_BRAIN_IDLE_CLOSE_SEC", "60"))
MAX_THREADS = int(os.environ.get("CHDB_BRAIN_MAX_THREADS", "2"))
MAX_QUERY_MEM_BYTES = int(os.environ.get("CHDB_BRAIN_MAX_QUERY_MEM_BYTES", str(256 * 1024 * 1024)))
EXTERNAL_SPILL_BYTES = max(16 * 1024 * 1024, MAX_QUERY_MEM_BYTES // 4)

S3_ENDPOINT = os.environ.get("CHDB_BRAIN_S3_ENDPOINT", "").rstrip("/")
S3_BUCKET = os.environ.get("CHDB_BRAIN_S3_BUCKET", "").strip("/")
S3_PREFIX = os.environ.get("CHDB_BRAIN_S3_PREFIX", "").strip("/")
S3_ACCESS_KEY = os.environ.get("CHDB_BRAIN_S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("CHDB_BRAIN_S3_SECRET_KEY", "")

if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", DB_NAME):
    raise ValueError(f"Invalid CHDB_BRAIN_DB_NAME: {DB_NAME!r}")

Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

_SESSION: chs.Session | None = None
_LAST_USED: float = 0.0
_SCHEMA_INITIALIZED: bool = False
_LOCK = asyncio.Lock()

try:
    _LIBC = ctypes.CDLL("libc.so.6")
except OSError:
    _LIBC = None

READ_ONLY_PREFIXES = ("SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN")


def _trim_memory() -> None:
    gc.collect()
    if _LIBC is not None:
        with contextlib.suppress(Exception):
            _LIBC.malloc_trim(0)


def _apply_low_mem_settings(session: chs.Session) -> None:
    for stmt in (
        f"SET max_threads = {MAX_THREADS}",
        f"SET max_memory_usage = {MAX_QUERY_MEM_BYTES}",
        "SET max_block_size = 8192",
        f"SET max_bytes_before_external_sort = {EXTERNAL_SPILL_BYTES}",
        f"SET max_bytes_before_external_group_by = {EXTERNAL_SPILL_BYTES}",
        "SET use_uncompressed_cache = 0",
    ):
        with contextlib.suppress(Exception):
            session.query(stmt, "CSV")


def _ensure_session_sync() -> chs.Session:
    global _SESSION, _LAST_USED, _SCHEMA_INITIALIZED
    if _SESSION is None:
        _SESSION = chs.Session(DATA_DIR)
        _apply_low_mem_settings(_SESSION)
        if not _SCHEMA_INITIALIZED:
            _init_schema_sync(_SESSION)
            _SCHEMA_INITIALIZED = True
    _LAST_USED = time.monotonic()
    return _SESSION


def _close_session_sync() -> None:
    global _SESSION
    if _SESSION is not None:
        with contextlib.suppress(Exception):
            _SESSION.close()
        _SESSION = None
        _trim_memory()


def _sql_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _sql_str_array(values: list[str] | None) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(_sql_quote(str(v)) for v in values) + "]"


def _utc_now_str() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _init_schema_sync(session: chs.Session) -> None:
    session.query(f"CREATE DATABASE IF NOT EXISTS {DB_NAME}")
    session.query(
        f"""
        CREATE TABLE IF NOT EXISTS {DB_NAME}.memories (
            id String,
            source LowCardinality(String),
            agent LowCardinality(String),
            workspace String,
            category LowCardinality(String),
            tags Array(String),
            title String,
            content String,
            metadata String,
            created_at DateTime64(3, 'UTC')
        )
        ENGINE = MergeTree
        ORDER BY (source, agent, workspace, created_at, id)
        """
    )
    session.query(
        f"""
        CREATE TABLE IF NOT EXISTS {DB_NAME}.transcripts (
            source LowCardinality(String),
            agent LowCardinality(String),
            conversation_id String,
            entry_id String,
            role LowCardinality(String),
            workspace String,
            content String,
            timestamp String,
            ingested_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(ingested_at)
        ORDER BY (source, agent, conversation_id, entry_id)
        """
    )


def _query_sync(sql: str, fmt: str) -> str:
    session = _ensure_session_sync()
    return str(session.query(sql, fmt))


async def _run_query(sql: str, fmt: str = "JSONEachRow") -> str:
    async with _LOCK:
        return await asyncio.to_thread(_query_sync, sql, fmt)


def _parse_json_each_row(raw: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


async def _insert_memory(
    *,
    content: str,
    title: str = "",
    source: str = "host",
    agent: str = "unknown",
    workspace: str = "",
    category: str = "note",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mem_id = str(uuid.uuid4())
    now = _utc_now_str()
    meta_json = json.dumps(metadata or {}, ensure_ascii=False)
    sql = (
        f"INSERT INTO {DB_NAME}.memories "
        f"(id, source, agent, workspace, category, tags, title, content, metadata, created_at) "
        f"VALUES ("
        f"{_sql_quote(mem_id)}, "
        f"{_sql_quote(source)}, "
        f"{_sql_quote(agent)}, "
        f"{_sql_quote(workspace)}, "
        f"{_sql_quote(category)}, "
        f"{_sql_str_array(tags)}, "
        f"{_sql_quote(title)}, "
        f"{_sql_quote(content)}, "
        f"{_sql_quote(meta_json)}, "
        f"{_sql_quote(now)}"
        f")"
    )
    await _run_query(sql, "CSV")
    return {
        "id": mem_id,
        "source": source,
        "agent": agent,
        "workspace": workspace,
        "category": category,
        "tags": tags or [],
        "title": title,
        "created_at": now,
    }


async def _ingest_transcript_rows(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    now = _utc_now_str()
    values_sql: list[str] = []
    for row in rows:
        values_sql.append(
            "("
            f"{_sql_quote(str(row.get('source', 'host')))}, "
            f"{_sql_quote(str(row.get('agent', 'agy-cli')))}, "
            f"{_sql_quote(str(row.get('conversation_id', '')))}, "
            f"{_sql_quote(str(row.get('entry_id', '')))}, "
            f"{_sql_quote(str(row.get('role', '')))}, "
            f"{_sql_quote(str(row.get('workspace', '')))}, "
            f"{_sql_quote(str(row.get('content', '')))}, "
            f"{_sql_quote(str(row.get('timestamp', '')))}, "
            f"{_sql_quote(now)}"
            ")"
        )
    sql = (
        f"INSERT INTO {DB_NAME}.transcripts "
        "(source, agent, conversation_id, entry_id, role, workspace, "
        "content, timestamp, ingested_at) "
        f"VALUES {', '.join(values_sql)}"
    )
    await _run_query(sql, "CSV")
    return len(values_sql)


mcp = FastMCP("chdb-brain", stateless_http=True)


@mcp.tool()
async def brain_remember(
    content: str,
    title: str = "",
    source: str = "host",
    agent: str = "unknown",
    workspace: str = "",
    category: str = "note",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Store a durable memory in the shared chDB brain.

    Args:
        content: The fact, decision, fix, preference, or note to remember.
        title: Optional short title for quick scanning.
        source: Environment origin, e.g. 'host' or 'vm'.
        agent: Agent/provider name, e.g. 'agy-cli', 'claude', or 'codex'.
        workspace: Repository or project path/name.
        category: Memory category, e.g. 'fact', 'decision', 'preference', 'fix', 'note'.
        tags: Optional list of keyword tags.
        metadata: Optional structured JSON metadata.
    """
    return await _insert_memory(
        content=content,
        title=title,
        source=source,
        agent=agent,
        workspace=workspace,
        category=category,
        tags=tags,
        metadata=metadata,
    )


@mcp.tool()
async def brain_search(
    query: str,
    source: str = "",
    agent: str = "",
    workspace: str = "",
    category: str = "",
    include_transcripts: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    """Search shared memories (and optionally mirrored transcripts) across agents/environments."""
    limit = max(1, min(int(limit), 200))
    q_lit = _sql_quote(query)

    mem_filters = [
        f"(positionCaseInsensitive(content, {q_lit}) > 0 "
        f"OR positionCaseInsensitive(title, {q_lit}) > 0 "
        f"OR has(tags, {q_lit}))"
    ]
    if source:
        mem_filters.append(f"source = {_sql_quote(source)}")
    if agent:
        mem_filters.append(f"agent = {_sql_quote(agent)}")
    if workspace:
        mem_filters.append(f"positionCaseInsensitive(workspace, {_sql_quote(workspace)}) > 0")
    if category:
        mem_filters.append(f"category = {_sql_quote(category)}")

    mem_sql = (
        f"SELECT id, source, agent, workspace, category, tags, title, content, created_at "
        f"FROM {DB_NAME}.memories "
        f"WHERE {' AND '.join(mem_filters)} "
        f"ORDER BY created_at DESC "
        f"LIMIT {limit}"
    )
    memories = _parse_json_each_row(await _run_query(mem_sql))
    result: dict[str, Any] = {"memories": memories}

    if include_transcripts:
        tx_filters = [f"positionCaseInsensitive(content, {q_lit}) > 0"]
        if source:
            tx_filters.append(f"source = {_sql_quote(source)}")
        if agent:
            tx_filters.append(f"agent = {_sql_quote(agent)}")
        if workspace:
            tx_filters.append(f"positionCaseInsensitive(workspace, {_sql_quote(workspace)}) > 0")

        tx_sql = (
            f"SELECT source, agent, conversation_id, entry_id, role, workspace, "
            f"substring(content, 1, 1000) AS content, timestamp "
            f"FROM {DB_NAME}.transcripts FINAL "
            f"WHERE {' AND '.join(tx_filters)} "
            f"ORDER BY ingested_at DESC "
            f"LIMIT {limit}"
        )
        result["transcripts"] = _parse_json_each_row(await _run_query(tx_sql))

    return result


@mcp.tool()
async def brain_recent(
    limit: int = 20,
    source: str = "",
    agent: str = "",
    workspace: str = "",
) -> list[dict[str, Any]]:
    """List the most recent memories, optionally filtered by source, agent, or workspace."""
    limit = max(1, min(int(limit), 200))
    filters = ["1 = 1"]
    if source:
        filters.append(f"source = {_sql_quote(source)}")
    if agent:
        filters.append(f"agent = {_sql_quote(agent)}")
    if workspace:
        filters.append(f"positionCaseInsensitive(workspace, {_sql_quote(workspace)}) > 0")

    sql = (
        f"SELECT id, source, agent, workspace, category, tags, title, content, created_at "
        f"FROM {DB_NAME}.memories "
        f"WHERE {' AND '.join(filters)} "
        f"ORDER BY created_at DESC "
        f"LIMIT {limit}"
    )
    return _parse_json_each_row(await _run_query(sql))


@mcp.tool()
async def brain_query(sql: str, output_format: str = "JSONEachRow") -> str:
    """Run read-only ClickHouse SQL against the chDB brain (supports table functions too)."""
    stripped = sql.strip().lstrip("(")
    first_word = stripped.split(None, 1)[0].upper() if stripped else ""
    if first_word not in READ_ONLY_PREFIXES:
        raise ValueError(
            f"brain_query only allows read-only statements ({', '.join(READ_ONLY_PREFIXES)}). "
            "Use brain_remember to store memories."
        )
    return await _run_query(sql, output_format)


@mcp.tool()
async def brain_stats() -> dict[str, Any]:
    """Return memory and transcript counts grouped by (source, agent)."""
    mem_sql = (
        f"SELECT source, agent, count() AS count "
        f"FROM {DB_NAME}.memories GROUP BY source, agent ORDER BY count DESC"
    )
    tx_sql = (
        f"SELECT source, agent, count() AS count "
        f"FROM {DB_NAME}.transcripts FINAL GROUP BY source, agent ORDER BY count DESC"
    )
    return {
        "chdb_version": getattr(chdb, "__version__", "unknown"),
        "database": DB_NAME,
        "s3_configured": bool(S3_ENDPOINT and S3_BUCKET),
        "memories": _parse_json_each_row(await _run_query(mem_sql)),
        "transcripts": _parse_json_each_row(await _run_query(tx_sql)),
    }


def _s3_object_url(filename: str) -> str:
    if not (S3_ENDPOINT and S3_BUCKET and S3_ACCESS_KEY and S3_SECRET_KEY):
        raise ValueError(
            "S3 backup is not configured. Set CHDB_BRAIN_S3_ENDPOINT, CHDB_BRAIN_S3_BUCKET, "
            "CHDB_BRAIN_S3_ACCESS_KEY, and CHDB_BRAIN_S3_SECRET_KEY in .env."
        )
    parts = [S3_ENDPOINT, S3_BUCKET]
    if S3_PREFIX:
        parts.append(S3_PREFIX)
    parts.append(filename)
    return "/".join(parts)


async def _backup_to_s3() -> dict[str, Any]:
    mem_url = _s3_object_url("memories.parquet")
    tx_url = _s3_object_url("transcripts.parquet")
    await _run_query(
        f"INSERT INTO FUNCTION s3({_sql_quote(mem_url)}, {_sql_quote(S3_ACCESS_KEY)}, "
        f"{_sql_quote(S3_SECRET_KEY)}, 'Parquet') "
        f"SETTINGS s3_truncate_on_insert = 1 "
        f"SELECT * FROM {DB_NAME}.memories",
        "CSV",
    )
    await _run_query(
        f"INSERT INTO FUNCTION s3({_sql_quote(tx_url)}, {_sql_quote(S3_ACCESS_KEY)}, "
        f"{_sql_quote(S3_SECRET_KEY)}, 'Parquet') "
        f"SETTINGS s3_truncate_on_insert = 1 "
        f"SELECT * FROM {DB_NAME}.transcripts FINAL",
        "CSV",
    )
    return {"status": "backed_up", "memories_url": mem_url, "transcripts_url": tx_url}


async def _restore_from_s3(replace: bool = False) -> dict[str, Any]:
    mem_url = _s3_object_url("memories.parquet")
    tx_url = _s3_object_url("transcripts.parquet")
    if replace:
        await _run_query(f"TRUNCATE TABLE {DB_NAME}.memories", "CSV")
        await _run_query(f"TRUNCATE TABLE {DB_NAME}.transcripts", "CSV")
    await _run_query(
        f"INSERT INTO {DB_NAME}.memories "
        f"SELECT * FROM s3({_sql_quote(mem_url)}, {_sql_quote(S3_ACCESS_KEY)}, "
        f"{_sql_quote(S3_SECRET_KEY)}, 'Parquet')",
        "CSV",
    )
    await _run_query(
        f"INSERT INTO {DB_NAME}.transcripts "
        f"SELECT * FROM s3({_sql_quote(tx_url)}, {_sql_quote(S3_ACCESS_KEY)}, "
        f"{_sql_quote(S3_SECRET_KEY)}, 'Parquet')",
        "CSV",
    )
    return {
        "status": "restored",
        "replace": replace,
        "memories_url": mem_url,
        "transcripts_url": tx_url,
    }


@mcp.tool()
async def brain_backup() -> dict[str, Any]:
    """Export memories and transcripts as Parquet snapshots to the configured S3/RustFS bucket."""
    return await _backup_to_s3()


@mcp.tool()
async def brain_restore(replace: bool = False) -> dict[str, Any]:
    """Restore memories and transcripts from the configured S3/RustFS Parquet snapshots."""
    return await _restore_from_s3(replace=replace)


async def healthz(request: Request) -> JSONResponse:
    if request.query_params.get("deep", "false").lower() == "true":
        await _run_query("SELECT 1", "CSV")
    return JSONResponse(
        {
            "status": "ok",
            "database": DB_NAME,
            "chdb_version": getattr(chdb, "__version__", "unknown"),
            "session_open": _SESSION is not None,
            "idle_close_sec": IDLE_CLOSE_SEC,
        }
    )


async def api_create_memory(request: Request) -> JSONResponse:
    body = await request.json()
    content = str(body.get("content", "")).strip()
    if not content:
        return JSONResponse({"error": "content is required"}, status_code=400)
    saved = await _insert_memory(
        content=content,
        title=str(body.get("title", "")),
        source=str(body.get("source", "host")),
        agent=str(body.get("agent", "unknown")),
        workspace=str(body.get("workspace", "")),
        category=str(body.get("category", "note")),
        tags=body.get("tags") or [],
        metadata=body.get("metadata") or {},
    )
    return JSONResponse(saved, status_code=201)


async def api_search_memories(request: Request) -> JSONResponse:
    q = request.query_params.get("q", "")
    source = request.query_params.get("source", "")
    agent = request.query_params.get("agent", "")
    workspace = request.query_params.get("workspace", "")
    include_transcripts = request.query_params.get("include_transcripts", "false").lower() == "true"
    limit = int(request.query_params.get("limit", "20"))
    if q:
        res = await brain_search(
            query=q,
            source=source,
            agent=agent,
            workspace=workspace,
            include_transcripts=include_transcripts,
            limit=limit,
        )
        return JSONResponse(res)
    recent = await brain_recent(limit=limit, source=source, agent=agent, workspace=workspace)
    return JSONResponse({"memories": recent})


async def api_ingest_transcripts(request: Request) -> JSONResponse:
    body = await request.json()
    rows = body.get("rows", [])
    if not isinstance(rows, list):
        return JSONResponse({"error": "rows must be a list"}, status_code=400)
    inserted = await _ingest_transcript_rows(rows)
    return JSONResponse({"ingested": inserted}, status_code=200)


async def api_backup(_request: Request) -> JSONResponse:
    try:
        return JSONResponse(await _backup_to_s3(), status_code=200)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def api_restore(request: Request) -> JSONResponse:
    replace = request.query_params.get("replace", "false").lower() == "true"
    try:
        return JSONResponse(await _restore_from_s3(replace=replace), status_code=200)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def _idle_reaper() -> None:
    if IDLE_CLOSE_SEC <= 0:
        return
    check_interval = max(5, min(15, IDLE_CLOSE_SEC // 2))
    while True:
        await asyncio.sleep(check_interval)
        if _SESSION is not None and (time.monotonic() - _LAST_USED) >= IDLE_CLOSE_SEC:
            async with _LOCK:
                if _SESSION is not None and (time.monotonic() - _LAST_USED) >= IDLE_CLOSE_SEC:
                    await asyncio.to_thread(_close_session_sync)


@contextlib.asynccontextmanager
async def lifespan(_app: Starlette):
    # Initialize schema once at startup, then release session immediately if idle_close enabled
    async with _LOCK:
        await asyncio.to_thread(_ensure_session_sync)
        if IDLE_CLOSE_SEC > 0:
            await asyncio.to_thread(_close_session_sync)
    reaper_task = asyncio.create_task(_idle_reaper())
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        reaper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper_task
        async with _LOCK:
            await asyncio.to_thread(_close_session_sync)


app = Starlette(
    routes=[
        Route("/healthz", healthz, methods=["GET"]),
        Route("/api/memories", api_create_memory, methods=["POST"]),
        Route("/api/memories", api_search_memories, methods=["GET"]),
        Route("/api/ingest/transcripts", api_ingest_transcripts, methods=["POST"]),
        Route("/api/backup", api_backup, methods=["POST"]),
        Route("/api/restore", api_restore, methods=["POST"]),
        Mount("/", app=mcp.streamable_http_app()),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    uvicorn.run(app, host=LISTEN_IP, port=PORT, log_level="info")
