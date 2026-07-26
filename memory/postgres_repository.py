from __future__ import annotations

import json
import threading
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import uuid4

from memory.commands import MemoryTransition
from memory.domain import (
    MemoryEvidence,
    MemoryIndexOperation,
    MemoryItem,
    MemoryKind,
    MemoryOwnerScope,
    MemorySourceType,
    MemoryStatus,
    OwnerKey,
)
from memory.repository import (
    InvalidTransition,
    MemoryIndexOutboxEvent,
    MemoryNotFound,
    VersionConflict,
)
from runtime.db import connect, resolve_database_config, row_get


class PostgresMemoryRepository:
    """PostgreSQL truth store for governed long-term semantic memory."""

    SCHEMA_VERSION = 1

    def __init__(self, database_url: str | None = None) -> None:
        self.config = resolve_database_config(
            database_url,
            env_names=("MEMORY_DATABASE_URL", "DATABASE_URL"),
            purpose="semantic memory repository",
        )
        self._schema_lock = threading.Lock()
        self._init_schema()

    def _connect(self):
        return connect(self.config)

    def _init_schema(self) -> None:
        owner_values = _sql_values(MemoryOwnerScope)
        kind_values = _sql_values(MemoryKind)
        status_values = _sql_values(MemoryStatus)
        source_values = _sql_values(MemorySourceType)
        operation_values = _sql_values(MemoryIndexOperation)
        with self._schema_lock, closing(self._connect()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_schema_versions (
                    module TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
            """)
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS memory_items (
                    id TEXT PRIMARY KEY,
                    owner_scope TEXT NOT NULL CHECK (owner_scope IN ({owner_values})),
                    owner_id TEXT NOT NULL CHECK (length(btrim(owner_id)) > 0),
                    kind TEXT NOT NULL CHECK (kind IN ({kind_values})),
                    content TEXT NOT NULL CHECK (length(btrim(content)) > 0),
                    normalized_content TEXT NOT NULL CHECK (length(btrim(normalized_content)) > 0),
                    status TEXT NOT NULL CHECK (status IN ({status_values})),
                    confidence DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
                    salience DOUBLE PRECISION NOT NULL CHECK (salience BETWEEN 0 AND 1),
                    valid_from TIMESTAMPTZ NOT NULL,
                    valid_until TIMESTAMPTZ,
                    last_confirmed_at TIMESTAMPTZ,
                    supersedes_id TEXT REFERENCES memory_items(id),
                    version INTEGER NOT NULL CHECK (version > 0),
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    CHECK (valid_until IS NULL OR valid_until > valid_from)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_items_owner_status
                ON memory_items(owner_scope, owner_id, status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_items_exact
                ON memory_items(owner_scope, owner_id, kind, normalized_content)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_items_supersedes
                ON memory_items(supersedes_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_items_valid_until
                ON memory_items(valid_until)
            """)
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS memory_evidence (
                    id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE RESTRICT,
                    source_type TEXT NOT NULL CHECK (source_type IN ({source_values})),
                    source_ref TEXT NOT NULL DEFAULT '',
                    session_id TEXT,
                    task_id TEXT,
                    workspace_id TEXT,
                    project_id TEXT,
                    excerpt TEXT NOT NULL DEFAULT '',
                    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_evidence_memory
                ON memory_evidence(memory_id, created_at)
            """)
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS memory_index_outbox (
                    id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE RESTRICT,
                    memory_version INTEGER NOT NULL,
                    operation TEXT NOT NULL CHECK (operation IN ({operation_values})),
                    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'retry', 'completed')),
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    next_attempt_at TIMESTAMPTZ NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    UNIQUE(memory_id, memory_version, operation)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_outbox_ready
                ON memory_index_outbox(status, next_attempt_at, created_at)
            """)
            now = datetime.now(timezone.utc)
            conn.execute(
                """
                INSERT INTO memory_schema_versions(module, version, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (module) DO UPDATE
                SET version = GREATEST(memory_schema_versions.version, EXCLUDED.version),
                    updated_at = EXCLUDED.updated_at
                """,
                ("semantic_memory", self.SCHEMA_VERSION, now),
            )
            conn.commit()

    def create(
        self,
        item: MemoryItem,
        evidence: Sequence[MemoryEvidence] = (),
    ) -> MemoryItem:
        with closing(self._connect()) as conn:
            try:
                self._insert_item(conn, item)
                self._insert_evidence(conn, item.id, evidence)
                if item.status is MemoryStatus.ACTIVE:
                    self._schedule(conn, item, MemoryIndexOperation.UPSERT)
                conn.commit()
            except Exception as exc:
                conn.rollback()
                if _is_unique_violation(exc):
                    raise VersionConflict(f"Memory already exists: {item.id}") from exc
                raise
        return item

    def get(self, memory_id: str) -> MemoryItem | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM memory_items WHERE id = %s",
                (str(memory_id),),
            ).fetchone()
        return _item_from_row(row) if row else None

    def get_many(self, memory_ids: Sequence[str]) -> list[MemoryItem]:
        ids = list(dict.fromkeys(str(value) for value in memory_ids if str(value)))
        if not ids:
            return []
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM memory_items WHERE id = ANY(%s)",
                (ids,),
            ).fetchall()
        by_id = {str(row_get(row, "id")): _item_from_row(row) for row in rows}
        return [by_id[value] for value in ids if value in by_id]

    def list_active(
        self,
        scopes: Sequence[OwnerKey],
        now: datetime,
    ) -> list[MemoryItem]:
        owners = list(dict.fromkeys(OwnerKey(value.scope, value.id) for value in scopes))
        if not owners:
            return []
        clauses = []
        params: list[Any] = []
        for owner in owners:
            clauses.append("(owner_scope = %s AND owner_id = %s)")
            params.extend((owner.scope.value, owner.id))
        params.extend((now, now))
        query = f"""
            SELECT * FROM memory_items
            WHERE ({' OR '.join(clauses)})
              AND status = 'active'
              AND valid_from <= %s
              AND (valid_until IS NULL OR valid_until > %s)
            ORDER BY updated_at DESC, id
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_item_from_row(row) for row in rows]

    def find_exact(
        self,
        owner: OwnerKey,
        kind: MemoryKind,
        normalized: str,
    ) -> MemoryItem | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM memory_items
                WHERE owner_scope = %s
                  AND owner_id = %s
                  AND kind = %s
                  AND normalized_content = %s
                  AND status IN ('candidate', 'active')
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (owner.scope.value, owner.id, MemoryKind(kind).value, str(normalized)),
            ).fetchone()
        return _item_from_row(row) if row else None

    def list_all_active(self, now: datetime) -> list[MemoryItem]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_items
                WHERE status = 'active'
                  AND valid_from <= %s
                  AND (valid_until IS NULL OR valid_until > %s)
                ORDER BY owner_scope, owner_id, kind, updated_at DESC, id
                """,
                (now, now),
            ).fetchall()
        return [_item_from_row(row) for row in rows]

    def transition(self, command: MemoryTransition) -> MemoryItem:
        with closing(self._connect()) as conn:
            try:
                row = conn.execute(
                    "SELECT * FROM memory_items WHERE id = %s FOR UPDATE",
                    (command.memory_id,),
                ).fetchone()
                if row is None:
                    raise MemoryNotFound(command.memory_id)
                item = _item_from_row(row)
                if item.version != command.expected_version:
                    raise VersionConflict(command.memory_id)
                try:
                    updated = item.transitioned(
                        command.target_status,
                        metadata={**command.metadata, "reason": command.reason},
                    )
                except ValueError as exc:
                    raise InvalidTransition(str(exc)) from exc
                self._update_item(conn, updated, expected_version=item.version)
                operation = (
                    MemoryIndexOperation.UPSERT
                    if updated.status is MemoryStatus.ACTIVE
                    else MemoryIndexOperation.DELETE
                )
                self._schedule(conn, updated, operation)
                conn.commit()
                return updated
            except Exception:
                conn.rollback()
                raise

    def supersede(
        self,
        old_memory_id: str,
        new_item: MemoryItem,
        evidence: Sequence[MemoryEvidence],
        *,
        expected_version: int,
    ) -> MemoryItem:
        with closing(self._connect()) as conn:
            try:
                row = conn.execute(
                    "SELECT * FROM memory_items WHERE id = %s FOR UPDATE",
                    (old_memory_id,),
                ).fetchone()
                if row is None:
                    raise MemoryNotFound(old_memory_id)
                old = _item_from_row(row)
                if old.version != expected_version:
                    raise VersionConflict(old_memory_id)
                if new_item.supersedes_id != old.id or new_item.owner != old.owner:
                    raise InvalidTransition("Invalid superseding memory chain.")
                try:
                    superseded = old.transitioned(MemoryStatus.SUPERSEDED)
                except ValueError as exc:
                    raise InvalidTransition(str(exc)) from exc
                self._update_item(conn, superseded, expected_version=old.version)
                self._insert_item(conn, new_item)
                self._insert_evidence(conn, new_item.id, evidence)
                self._schedule(conn, superseded, MemoryIndexOperation.DELETE)
                self._schedule(conn, new_item, MemoryIndexOperation.UPSERT)
                conn.commit()
                return new_item
            except Exception:
                conn.rollback()
                raise

    def add_evidence(
        self,
        memory_id: str,
        evidence: Sequence[MemoryEvidence],
        *,
        expected_version: int | None = None,
    ) -> MemoryItem:
        with closing(self._connect()) as conn:
            try:
                row = conn.execute(
                    "SELECT * FROM memory_items WHERE id = %s FOR UPDATE",
                    (memory_id,),
                ).fetchone()
                if row is None:
                    raise MemoryNotFound(memory_id)
                item = _item_from_row(row)
                if expected_version is not None and item.version != expected_version:
                    raise VersionConflict(memory_id)
                self._insert_evidence(conn, item.id, evidence)
                changed_at = datetime.now(timezone.utc)
                conn.execute(
                    "UPDATE memory_items SET updated_at = %s WHERE id = %s",
                    (changed_at, item.id),
                )
                conn.commit()
                return replace(item, updated_at=changed_at)
            except Exception:
                conn.rollback()
                raise

    def list_evidence(self, memory_id: str) -> list[MemoryEvidence]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM memory_evidence WHERE memory_id = %s ORDER BY created_at, id",
                (memory_id,),
            ).fetchall()
        return [_evidence_from_row(row) for row in rows]

    def claim_index_events(self, limit: int = 100) -> list[MemoryIndexOutboxEvent]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                WITH ready AS (
                    SELECT id
                    FROM memory_index_outbox
                    WHERE status IN ('pending', 'retry')
                      AND next_attempt_at <= now()
                    ORDER BY created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE memory_index_outbox AS target
                SET status = 'processing', updated_at = now()
                FROM ready
                WHERE target.id = ready.id
                RETURNING target.*
                """,
                (max(1, int(limit)),),
            ).fetchall()
            conn.commit()
        return [_outbox_from_row(row) for row in rows]

    def complete_index_event(self, event_id: str) -> None:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                UPDATE memory_index_outbox
                SET status = 'completed', updated_at = now(), last_error = ''
                WHERE id = %s
                """,
                (event_id,),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                raise MemoryNotFound(f"Outbox event: {event_id}")
            conn.commit()

    def retry_index_event(
        self,
        event_id: str,
        *,
        error: str,
        next_attempt_at: datetime,
    ) -> None:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                UPDATE memory_index_outbox
                SET status = 'retry',
                    attempt_count = attempt_count + 1,
                    next_attempt_at = %s,
                    last_error = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (next_attempt_at, str(error)[:1000], event_id),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                raise MemoryNotFound(f"Outbox event: {event_id}")
            conn.commit()

    def list_index_events(self, *, status: str | None = None) -> list[MemoryIndexOutboxEvent]:
        query = "SELECT * FROM memory_index_outbox"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = %s"
            params = (status,)
        query += " ORDER BY created_at, id"
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_outbox_from_row(row) for row in rows]

    def _insert_item(self, conn, item: MemoryItem) -> None:
        conn.execute(
            """
            INSERT INTO memory_items (
                id, owner_scope, owner_id, kind, content, normalized_content,
                status, confidence, salience, valid_from, valid_until,
                last_confirmed_at, supersedes_id, version, created_at, updated_at,
                metadata
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s::jsonb
            )
            """,
            _item_values(item),
        )

    def _update_item(self, conn, item: MemoryItem, *, expected_version: int) -> None:
        cursor = conn.execute(
            """
            UPDATE memory_items
            SET status = %s,
                confidence = %s,
                salience = %s,
                valid_until = %s,
                last_confirmed_at = %s,
                version = %s,
                updated_at = %s,
                metadata = %s::jsonb
            WHERE id = %s AND version = %s
            """,
            (
                item.status.value,
                item.confidence,
                item.salience,
                item.valid_until,
                item.last_confirmed_at,
                item.version,
                item.updated_at,
                json.dumps(item.metadata, ensure_ascii=False, default=str),
                item.id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise VersionConflict(item.id)

    def _insert_evidence(
        self,
        conn,
        memory_id: str,
        evidence: Sequence[MemoryEvidence],
    ) -> None:
        for value in evidence:
            item = replace(value, memory_id=memory_id)
            conn.execute(
                """
                INSERT INTO memory_evidence (
                    id, memory_id, source_type, source_ref, session_id, task_id,
                    workspace_id, project_id, excerpt, metadata, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s::jsonb, %s
                )
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    item.id,
                    item.memory_id,
                    item.source_type.value,
                    item.source_ref,
                    item.session_id,
                    item.task_id,
                    item.workspace_id,
                    item.project_id,
                    item.excerpt,
                    json.dumps(item.metadata, ensure_ascii=False, default=str),
                    item.created_at,
                ),
            )

    def _schedule(
        self,
        conn,
        item: MemoryItem,
        operation: MemoryIndexOperation,
    ) -> None:
        now = datetime.now(timezone.utc)
        conn.execute(
            """
            INSERT INTO memory_index_outbox (
                id, memory_id, memory_version, operation, status,
                attempt_count, next_attempt_at, last_error, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, 'pending', 0, %s, '', %s, %s)
            ON CONFLICT (memory_id, memory_version, operation) DO NOTHING
            """,
            (str(uuid4()), item.id, item.version, operation.value, now, now, now),
        )


def _item_values(item: MemoryItem) -> tuple[Any, ...]:
    return (
        item.id,
        item.owner_scope.value,
        item.owner_id,
        item.kind.value,
        item.content,
        item.normalized_content,
        item.status.value,
        item.confidence,
        item.salience,
        item.valid_from,
        item.valid_until,
        item.last_confirmed_at,
        item.supersedes_id,
        item.version,
        item.created_at,
        item.updated_at,
        json.dumps(item.metadata, ensure_ascii=False, default=str),
    )


def _item_from_row(row) -> MemoryItem:
    return MemoryItem(
        id=str(row_get(row, "id")),
        owner_scope=MemoryOwnerScope(row_get(row, "owner_scope")),
        owner_id=str(row_get(row, "owner_id")),
        kind=MemoryKind(row_get(row, "kind")),
        content=str(row_get(row, "content")),
        normalized_content=str(row_get(row, "normalized_content")),
        status=MemoryStatus(row_get(row, "status")),
        confidence=float(row_get(row, "confidence")),
        salience=float(row_get(row, "salience")),
        valid_from=row_get(row, "valid_from"),
        valid_until=row_get(row, "valid_until"),
        last_confirmed_at=row_get(row, "last_confirmed_at"),
        supersedes_id=row_get(row, "supersedes_id"),
        version=int(row_get(row, "version")),
        created_at=row_get(row, "created_at"),
        updated_at=row_get(row, "updated_at"),
        metadata=_json_object(row_get(row, "metadata")),
    )


def _evidence_from_row(row) -> MemoryEvidence:
    return MemoryEvidence(
        id=str(row_get(row, "id")),
        memory_id=str(row_get(row, "memory_id")),
        source_type=MemorySourceType(row_get(row, "source_type")),
        source_ref=str(row_get(row, "source_ref") or ""),
        session_id=row_get(row, "session_id"),
        task_id=row_get(row, "task_id"),
        workspace_id=row_get(row, "workspace_id"),
        project_id=row_get(row, "project_id"),
        excerpt=str(row_get(row, "excerpt") or ""),
        metadata=_json_object(row_get(row, "metadata")),
        created_at=row_get(row, "created_at"),
    )


def _outbox_from_row(row) -> MemoryIndexOutboxEvent:
    return MemoryIndexOutboxEvent(
        id=str(row_get(row, "id")),
        memory_id=str(row_get(row, "memory_id")),
        memory_version=int(row_get(row, "memory_version")),
        operation=MemoryIndexOperation(row_get(row, "operation")),
        status=str(row_get(row, "status")),
        attempt_count=int(row_get(row, "attempt_count")),
        next_attempt_at=row_get(row, "next_attempt_at"),
        last_error=str(row_get(row, "last_error") or ""),
        created_at=row_get(row, "created_at"),
        updated_at=row_get(row, "updated_at"),
    )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _sql_values(enum_type) -> str:
    return ", ".join("'" + value.value.replace("'", "''") + "'" for value in enum_type)


def _is_unique_violation(exc: Exception) -> bool:
    return exc.__class__.__name__ == "UniqueViolation"
