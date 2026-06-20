from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4


POSTGRES_SCHEMES = ("postgres://", "postgresql://")


@contextmanager
def temporary_postgres_schema(prefix: str = "test") -> Iterator[str]:
    dsn = _database_url()
    if not dsn:
        raise unittest.SkipTest("DATABASE_URL is required for PostgreSQL store tests.")
    try:
        import psycopg
    except ImportError as exc:
        raise unittest.SkipTest("psycopg is required for PostgreSQL store tests.") from exc

    schema = f"{_clean_prefix(prefix)}_{uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield _dsn_with_search_path(dsn, schema)
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _database_url() -> str:
    value = str(os.getenv("DATABASE_URL") or "").strip()
    if _is_postgres_url(value):
        return value
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        return ""
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        if key.strip() != "DATABASE_URL":
            continue
        value = _clean_env_value(raw_value.strip())
        return value if _is_postgres_url(value) else ""
    return ""


def _dsn_with_search_path(dsn: str, schema: str) -> str:
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = f"-c search_path={schema}"
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(query, quote_via=quote),
        parts.fragment,
    ))


def _is_postgres_url(value: str) -> bool:
    return value.lower().startswith(POSTGRES_SCHEMES)


def _clean_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _clean_prefix(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in value)
    return cleaned.strip("_") or "test"
