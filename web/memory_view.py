"""Read-only, scoped projection of the semantic memory truth store.

The console must not bootstrap the agent, call embeddings, or create tables just
to inspect memories. Context owners come from an authenticated stored session;
arbitrary owner IDs supplied by the browser are never accepted here.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from memory.commands import MemoryContext
from memory.domain import MemoryKind, MemoryOwnerScope, MemoryStatus, OwnerKey
from runtime.db import connect, resolve_database_config, row_get
from user_scope import normalize_user_id, parse_web_session_id, web_session_id


class MemoryContextUnavailable(RuntimeError):
    """The stored session cannot be read; the message is safe for HTTP clients."""


def load_memory_session(session_id: str, user_id: str) -> dict[str, Any]:
    """Load only metadata from this user's Web session without schema writes."""
    user_id = normalize_user_id(user_id)
    value = str(session_id or "").strip()
    if not value:
        raise ValueError("session_id is required.")
    parsed = parse_web_session_id(value)
    if parsed is not None:
        if parsed[0] != user_id:
            raise ValueError("Session does not belong to the current user.")
        storage_id = value
    elif ":" in value:
        raise ValueError("Only Web sessions owned by the current user can be accessed.")
    else:
        storage_id = web_session_id(user_id, value)
    try:
        stored = _load_stored_session(storage_id)
    except Exception:
        raise MemoryContextUnavailable("会话上下文暂不可用，请稍后刷新或选择个人长期记忆。") from None
    session = stored or {"id": storage_id, "metadata": {}}
    _authorized_owners(user_id, session)
    return session


def read_memory_overview(
    user_id: str,
    *,
    session: dict[str, Any] | None = None,
    limit: int = 200,
    offset: int = 0,
    query: str = "",
    status: str = "",
    kind: str = "",
) -> dict[str, Any]:
    """Return all lifecycle states for authorized owners, with scoped totals.

    Invalid user/session ownership raises ValueError. Persistence/configuration
    failures return ``available=False`` with a safe message, never a DSN or raw
    database exception. ``counts`` covers the entire authorized scope, while
    ``items`` is paginated. This operation has no database or index writes.
    """
    user_id = normalize_user_id(user_id)
    filters = _validated_filters(query, status, kind)
    owners = _authorized_owners(user_id, session)
    task_owners, context_message = _bound_task_owners(user_id, session)
    owners = tuple(dict.fromkeys((*owners, *task_owners)))
    mutable_owners = _mutable_owners(user_id, task_owners)
    page_limit = max(1, min(int(limit), 200))
    page_offset = max(0, int(offset))
    response: dict[str, Any] = {
        "backend": "postgres",
        "available": False,
        "items": [],
        "scopes": [{"scope": owner.scope.value, "id": owner.id} for owner in owners],
        "counts": {"total": 0, "retrievable": 0, "by_status": {}},
        "filtered_total": 0,
        "filters": filters,
        "has_more": False,
        "next_offset": None,
        "runtime": {
            "write_enabled": _env_bool("SEMANTIC_MEMORY_WRITE_ENABLED"),
            "read_enabled": _env_bool("SEMANTIC_MEMORY_READ_ENABLED"),
            "context_enabled": _env_bool("SEMANTIC_MEMORY_CONTEXT_ENABLED"),
        },
        "message": "",
        "context_message": context_message,
    }
    now = datetime.now(timezone.utc)
    clauses, owner_params = _scope_filter(owners)
    filtered_clauses, filter_params = _search_filter(clauses, owner_params, filters)
    try:
        config = resolve_database_config(
            None,
            env_names=("MEMORY_DATABASE_URL", "DATABASE_URL"),
            purpose="semantic memory console",
        )
        with closing(connect(config)) as conn:
            # Explicitly keep this projection read-only even if the configured
            # database user has write privileges.
            conn.execute("SET TRANSACTION READ ONLY")
            totals = conn.execute(
                f"""
                SELECT status, COUNT(*) AS count,
                    COUNT(*) FILTER (
                        WHERE status = 'active' AND valid_from <= %s
                        AND (valid_until IS NULL OR valid_until > %s)
                    ) AS retrievable
                FROM memory_items WHERE {clauses}
                GROUP BY status
                """,
                [now, now, *owner_params],
            ).fetchall()
            filtered_total = sum(int(row_get(row, "count", 0)) for row in totals)
            if any(filters.values()):
                count_row = conn.execute(
                    f"SELECT COUNT(*) AS count FROM memory_items WHERE {filtered_clauses}",
                    filter_params,
                ).fetchone()
                filtered_total = int(row_get(count_row, "count", 0))
            rows = conn.execute(
                f"""
                SELECT * FROM memory_items WHERE {filtered_clauses}
                ORDER BY updated_at DESC, id
                LIMIT %s OFFSET %s
                """,
                [*filter_params, page_limit + 1, page_offset],
            ).fetchall()
            has_more = len(rows) > page_limit
            rows = rows[:page_limit]
            ids = [str(row_get(row, "id")) for row in rows]
            evidence_rows = conn.execute(
                "SELECT * FROM memory_evidence WHERE memory_id = ANY(%s) ORDER BY created_at, id",
                (ids,),
            ).fetchall() if ids else []
        evidence_by_id: dict[str, list[dict[str, Any]]] = {}
        for row in evidence_rows:
            evidence_by_id.setdefault(str(row_get(row, "memory_id")), []).append(_evidence(row))
        by_status = {str(row_get(row, "status")): int(row_get(row, "count", 0)) for row in totals}
        response.update({
            "available": True,
            "items": [_item(row, evidence_by_id.get(str(row_get(row, "id")), []), now,
                            manageable=OwnerKey(row_get(row, "owner_scope"), row_get(row, "owner_id")) in mutable_owners)
                      for row in rows],
            "filtered_total": filtered_total,
            "counts": {
                "total": sum(by_status.values()),
                "retrievable": sum(int(row_get(row, "retrievable", 0)) for row in totals),
                "by_status": by_status,
            },
            "has_more": has_more,
            "next_offset": page_offset + len(rows) if has_more else None,
        })
    except Exception:
        response["message"] = "长期记忆存储暂不可用。请检查 PostgreSQL 连接及记忆表是否已初始化，然后刷新。"
    return response


def _authorized_owners(user_id: str, session: dict[str, Any] | None) -> tuple[OwnerKey, ...]:
    if session is None:
        return (OwnerKey(MemoryOwnerScope.USER, user_id),)
    parsed = parse_web_session_id(str(session.get("id") or ""))
    metadata = dict(session.get("metadata") or {})
    if parsed is None or parsed[0] != user_id or metadata.get("user_id", user_id) != user_id:
        raise ValueError("Memory context must belong to the current user.")
    metadata["user_id"] = user_id
    return MemoryContext.from_session(SimpleNamespace(id=session["id"], metadata=metadata)).allowed_owners()


def _bound_task_owners(user_id: str, session: dict[str, Any] | None) -> tuple[tuple[OwnerKey, ...], str]:
    metadata = (session or {}).get("metadata") or {}
    task_id = metadata.get("active_coding_task_id")
    if not task_id:
        return (), ""
    unavailable = "绑定编码任务的记忆作用域暂不可用；当前仅显示已验证的会话作用域。"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", str(task_id)):
        return (), unavailable
    try:
        task = _load_stored_session(f"task:{task_id}")
        task_metadata = (task or {}).get("metadata") or {}
        if (task is None or task.get("id") != f"task:{task_id}"
                or task_metadata.get("kind") != "coding_application"
                or task_metadata.get("task_id") != task_id
                or task_metadata.get("parent_session_id") != session["id"]
                or task_metadata.get("user_id") != user_id):
            return (), unavailable
        context = MemoryContext.from_session(SimpleNamespace(id=task["id"], metadata=task_metadata))
        return context.allowed_owners(), ""
    except Exception:
        return (), unavailable


def _load_stored_session(storage_id: str) -> dict[str, Any] | None:
    config = resolve_database_config(
        None, env_names=("SESSION_DATABASE_URL", "DATABASE_URL"), purpose="memory context console",
    )
    with closing(connect(config)) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        row = conn.execute("SELECT id, metadata FROM sessions WHERE id = %s", (storage_id,)).fetchone()
    if row is None:
        return None
    metadata = row_get(row, "metadata")
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    if not isinstance(metadata, dict):
        raise ValueError("Stored session metadata is invalid.")
    return {"id": str(row_get(row, "id")), "metadata": metadata}


def _scope_filter(owners: tuple[OwnerKey, ...]) -> tuple[str, list[str]]:
    clauses = " OR ".join("(owner_scope = %s AND owner_id = %s)" for _ in owners)
    params = [value for owner in owners for value in (owner.scope.value, owner.id)]
    return f"({clauses})", params


def _mutable_owners(user_id: str, verified_task_owners: tuple[OwnerKey, ...]) -> tuple[OwnerKey, ...]:
    # Shared application/workspace/project memories remain read-only. A task
    # supplied by parent metadata alone does not grant management authority.
    return (OwnerKey(MemoryOwnerScope.USER, user_id), *(
        owner for owner in verified_task_owners if owner.scope is MemoryOwnerScope.TASK
    ))


def _validated_filters(query: str, status: str, kind: str) -> dict[str, str]:
    if not all(isinstance(value, str) for value in (query, status, kind)):
        raise ValueError("Memory filters must be strings.")
    query = query.strip()
    if "\x00" in query:
        raise ValueError("Memory search contains an invalid character.")
    if len(query) > 500:
        raise ValueError("Memory search cannot exceed 500 characters.")
    if status and status not in {value.value for value in MemoryStatus}:
        raise ValueError("Invalid memory status.")
    if kind and kind not in {value.value for value in MemoryKind}:
        raise ValueError("Invalid memory kind.")
    return {"query": query, "status": status, "kind": kind}


def _search_filter(clauses: str, owner_params: list[str], filters: dict[str, str]) -> tuple[str, list[str]]:
    params = list(owner_params)
    if filters["query"]:
        # Escaping is separate from parameterization: %, _ and backslash must
        # remain literal substring characters rather than LIKE wildcards.
        escaped = filters["query"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses += " AND (content ILIKE %s ESCAPE E'\\\\' OR id ILIKE %s ESCAPE E'\\\\' OR owner_id ILIKE %s ESCAPE E'\\\\')"
        params.extend([f"%{escaped}%"] * 3)
    for field in ("status", "kind"):
        if filters[field]:
            clauses += f" AND {field} = %s"
            params.append(filters[field])
    return clauses, params


def _item(row: Any, evidence: list[dict[str, Any]], now: datetime, *, manageable: bool = False) -> dict[str, Any]:
    valid_from = row_get(row, "valid_from")
    valid_until = row_get(row, "valid_until")
    return {
        "id": str(row_get(row, "id")),
        "owner_scope": str(row_get(row, "owner_scope")),
        "owner_id": str(row_get(row, "owner_id")),
        "kind": str(row_get(row, "kind")),
        "content": str(row_get(row, "content")),
        "status": str(row_get(row, "status")),
        "confidence": float(row_get(row, "confidence", 0)),
        "salience": float(row_get(row, "salience", 0)),
        "version": int(row_get(row, "version", 1)),
        "manageable": manageable,
        "retrievable": row_get(row, "status") == "active"
            and valid_from <= now and (valid_until is None or valid_until > now),
        "valid_from": _date(valid_from),
        "valid_until": _date(valid_until),
        "last_confirmed_at": _date(row_get(row, "last_confirmed_at")),
        "created_at": _date(row_get(row, "created_at")),
        "updated_at": _date(row_get(row, "updated_at")),
        "supersedes_id": row_get(row, "supersedes_id"),
        "source_types": list(dict.fromkeys(value["source_type"] for value in evidence)),
        "evidence": evidence,
    }


def _evidence(row: Any) -> dict[str, Any]:
    return {
        "id": str(row_get(row, "id")),
        "source_type": str(row_get(row, "source_type")),
        "source_ref": str(row_get(row, "source_ref") or ""),
        "session_id": row_get(row, "session_id"),
        "task_id": row_get(row, "task_id"),
        "workspace_id": row_get(row, "workspace_id"),
        "project_id": row_get(row, "project_id"),
        "excerpt": str(row_get(row, "excerpt") or ""),
        "created_at": _date(row_get(row, "created_at")),
    }


def _date(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else str(value) if value else None


def _env_bool(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}
