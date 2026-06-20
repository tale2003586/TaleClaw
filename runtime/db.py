from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


POSTGRES_SCHEMES = ("postgres://", "postgresql://")


@dataclass(frozen=True)
class DatabaseConfig:
    dsn: str

    @property
    def is_postgres(self) -> bool:
        return True


def resolve_database_config(
    explicit_url: str | Path | None,
    *,
    env_names: Iterable[str],
    purpose: str = "database",
) -> DatabaseConfig:
    _load_dotenv_once()
    if explicit_url is not None:
        value = str(explicit_url).strip()
        if _is_postgres_url(value):
            return DatabaseConfig(dsn=value)
        raise ValueError(
            f"{purpose} requires a PostgreSQL DSN; local database paths are no longer supported."
        )

    checked_names = []
    for name in env_names:
        checked_names.append(name)
        value = str(os.getenv(name) or "").strip()
        if not value:
            continue
        if _is_postgres_url(value):
            return DatabaseConfig(dsn=value)
        raise ValueError(
            f"{name} must be a PostgreSQL DSN for {purpose}; got a non-PostgreSQL value."
        )

    names = ", ".join(checked_names) or "DATABASE_URL"
    raise RuntimeError(
        f"{purpose} requires PostgreSQL. Set one of: {names}."
    )


def connect(config: DatabaseConfig):
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError(
            "psycopg is required for PostgreSQL. Install requirements.txt first."
        ) from exc
    return psycopg.connect(config.dsn, row_factory=dict_row)


def sql(config: DatabaseConfig, statement: str) -> str:
    return statement.replace("?", "%s")


def execute_many(conn: Any, config: DatabaseConfig, statement: str, rows: Iterable[Iterable[Any]]) -> None:
    rows = list(rows)
    if not rows:
        return
    rendered = sql(config, statement)
    with conn.cursor() as cursor:
        cursor.executemany(rendered, rows)


def row_get(row: Any, key: str | int, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def is_integrity_error(exc: Exception) -> bool:
    return exc.__class__.__name__ == "UniqueViolation"


def _is_postgres_url(value: str) -> bool:
    return value.strip().lower().startswith(POSTGRES_SCHEMES)


_DOTENV_LOADED = False


def _load_dotenv_once() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        _DOTENV_LOADED = True
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _clean_env_value(value.strip())
    _DOTENV_LOADED = True


def _clean_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
