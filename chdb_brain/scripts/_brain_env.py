"""Shared config loader for chdb_brain host-side helper scripts (stdlib only).

Resolution order for each setting:
  1. Process environment
  2. chdb_brain/.env      (CHDB_BRAIN_*)
  3. rustfs/.env          (RUSTFS_* fallback for S3 endpoint/bucket/credentials)
  4. Built-in local defaults
"""

from __future__ import annotations

import os
import re
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVICE_DIR.parent

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def load_dotenv(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file with ${VAR} expansion."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        val = _VAR_RE.sub(
            lambda m: values.get(
                m.group(1) or m.group(2), os.environ.get(m.group(1) or m.group(2), "")
            ),
            val,
        )
        values[key] = val
    return values


def load_config() -> dict[str, str]:
    brain = load_dotenv(SERVICE_DIR / ".env")
    rustfs = load_dotenv(REPO_ROOT / "rustfs" / ".env")

    def pick(*candidates: tuple[dict[str, str], str], default: str = "") -> str:
        for source, key in candidates:
            if source.get(key):
                return source[key]
        return default

    env = dict(os.environ)
    connect_ip = pick(
        (env, "CHDB_BRAIN_CONNECT_IP"), (brain, "CHDB_BRAIN_CONNECT_IP"), default="127.0.0.1"
    )
    port = pick((env, "CHDB_BRAIN_PORT"), (brain, "CHDB_BRAIN_PORT"), default="8765")
    rustfs_endpoint = ""
    if rustfs.get("RUSTFS_CONNECT_IP") and rustfs.get("RUSTFS_S3_PORT"):
        rustfs_endpoint = f"http://{rustfs['RUSTFS_CONNECT_IP']}:{rustfs['RUSTFS_S3_PORT']}"

    return {
        "brain_url": pick(
            (env, "CHDB_BRAIN_BASE_URL"), default=f"http://{connect_ip}:{port}"
        ).rstrip("/"),
        "s3_endpoint": pick(
            (env, "CHDB_BRAIN_S3_ENDPOINT"),
            (brain, "CHDB_BRAIN_S3_ENDPOINT"),
            default=rustfs_endpoint or "http://127.0.0.1:9100",
        ).rstrip("/"),
        "s3_bucket": pick(
            (env, "CHDB_BRAIN_S3_BUCKET"),
            (brain, "CHDB_BRAIN_S3_BUCKET"),
            (rustfs, "RUSTFS_DEFAULT_BUCKET"),
            default="agent-brain",
        ).strip("/"),
        "s3_prefix": pick(
            (env, "CHDB_BRAIN_S3_PREFIX"), (brain, "CHDB_BRAIN_S3_PREFIX"), default="brains/vin"
        ).strip("/"),
        "s3_access_key": pick(
            (env, "CHDB_BRAIN_S3_ACCESS_KEY"),
            (brain, "CHDB_BRAIN_S3_ACCESS_KEY"),
            (rustfs, "RUSTFS_ACCESS_KEY"),
            default="rustfsadmin",
        ),
        "s3_secret_key": pick(
            (env, "CHDB_BRAIN_S3_SECRET_KEY"),
            (brain, "CHDB_BRAIN_S3_SECRET_KEY"),
            (rustfs, "RUSTFS_SECRET_KEY"),
            default="rustfsadmin",
        ),
    }


def sql_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def s3_table(cfg: dict[str, str], name: str) -> str:
    """Return a chDB s3() table function for <bucket>/<prefix>/<name>.parquet."""
    parts = [cfg["s3_endpoint"], cfg["s3_bucket"]]
    if cfg["s3_prefix"]:
        parts.append(cfg["s3_prefix"])
    parts.append(f"{name}.parquet")
    url = "/".join(parts)
    return (
        f"s3({sql_quote(url)}, {sql_quote(cfg['s3_access_key'])}, "
        f"{sql_quote(cfg['s3_secret_key'])}, 'Parquet')"
    )
