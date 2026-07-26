from __future__ import annotations

import json
from typing import Any

from runtime.db import connect, resolve_database_config

from ..models import MinecraftCheckpoint, MinecraftTask, MinecraftTaskEvent
from .memory import StoreConflict


_ACTIVE_STATUSES = (
    "pending",
    "connecting",
    "observing",
    "planning",
    "executing",
    "recovering",
)


class PostgresMinecraftTaskStore:
    """Persistent store with optimistic updates, leases, and additive migrations."""

    def __init__(self, dsn: str) -> None:
        self.config = resolve_database_config(
            dsn, env_names=(), purpose="Minecraft task store"
        )
        self._init_schema()

    def _init_schema(self) -> None:
        with connect(self.config) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS minecraft_tasks (
                    task_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    bot_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,
                    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
                    lease_owner TEXT,
                    lease_expires_at TIMESTAMPTZ,
                    task_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                "ALTER TABLE minecraft_tasks ADD COLUMN IF NOT EXISTS "
                "cancel_requested BOOLEAN NOT NULL DEFAULT FALSE"
            )
            conn.execute(
                "ALTER TABLE minecraft_tasks ADD COLUMN IF NOT EXISTS lease_owner TEXT"
            )
            conn.execute(
                "ALTER TABLE minecraft_tasks ADD COLUMN IF NOT EXISTS "
                "lease_expires_at TIMESTAMPTZ"
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_minecraft_active_bot
                ON minecraft_tasks (bot_id)
                WHERE status IN (
                    'pending','connecting','observing','planning','executing','recovering'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS minecraft_runs (
                    run_id BIGSERIAL PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES minecraft_tasks(task_id) ON DELETE CASCADE,
                    owner_id TEXT NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finished_at TIMESTAMPTZ
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS minecraft_task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES minecraft_tasks(task_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_minecraft_events_task_created "
                "ON minecraft_task_events (task_id, created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS minecraft_checkpoints (
                    task_id TEXT PRIMARY KEY REFERENCES minecraft_tasks(task_id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    plan_version INTEGER NOT NULL,
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL
                )
                """
            )

    def create(self, task: MinecraftTask, *, idempotency_key: str) -> MinecraftTask:
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        try:
            with connect(self.config) as conn:
                row = conn.execute(
                    """
                    INSERT INTO minecraft_tasks
                        (task_id,idempotency_key,bot_id,status,version,
                         cancel_requested,task_json,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                    ON CONFLICT (idempotency_key) DO UPDATE
                    SET idempotency_key=EXCLUDED.idempotency_key
                    RETURNING task_json,cancel_requested
                    """,
                    (
                        task.task_id,
                        key,
                        task.bot_id,
                        task.status.value,
                        task.version,
                        task.cancel_requested,
                        self._task_payload(task),
                        task.created_at,
                        task.updated_at,
                    ),
                ).fetchone()
        except Exception as exc:
            if exc.__class__.__name__ in {"UniqueViolation", "ExclusionViolation"}:
                raise StoreConflict(
                    f"bot {task.bot_id} already has an active task"
                ) from exc
            raise
        return self._task_from_row(row)

    def get(self, task_id: str) -> MinecraftTask | None:
        with connect(self.config) as conn:
            row = conn.execute(
                "SELECT task_json,cancel_requested FROM minecraft_tasks WHERE task_id=%s",
                (task_id,),
            ).fetchone()
        return self._task_from_row(row) if row else None

    def update(self, task: MinecraftTask, *, expected_version: int) -> MinecraftTask:
        updated = task.model_copy(
            update={"version": max(task.version, int(expected_version) + 1)}
        )
        try:
            with connect(self.config) as conn:
                row = conn.execute(
                    """
                    UPDATE minecraft_tasks SET
                        bot_id=%s,status=%s,version=%s,cancel_requested=%s,
                        task_json=%s::jsonb,updated_at=NOW()
                    WHERE task_id=%s AND version=%s
                    RETURNING task_json,cancel_requested
                    """,
                    (
                        updated.bot_id,
                        updated.status.value,
                        updated.version,
                        updated.cancel_requested,
                        self._task_payload(updated),
                        updated.task_id,
                        expected_version,
                    ),
                ).fetchone()
                if row is None:
                    exists = conn.execute(
                        "SELECT 1 FROM minecraft_tasks WHERE task_id=%s",
                        (task.task_id,),
                    ).fetchone()
                    if exists is None:
                        raise KeyError(task.task_id)
                    raise StoreConflict(f"version conflict for task {task.task_id}")
        except Exception as exc:
            if exc.__class__.__name__ == "UniqueViolation":
                raise StoreConflict(
                    f"bot {task.bot_id} already has an active task"
                ) from exc
            raise
        return self._task_from_row(row)

    def append_event(self, event: MinecraftTaskEvent) -> None:
        with connect(self.config) as conn:
            conn.execute(
                """
                INSERT INTO minecraft_task_events
                    (event_id,task_id,event_type,payload,created_at)
                VALUES (%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    event.event_id,
                    event.task_id,
                    event.event_type,
                    self._safe_json(event.payload),
                    event.created_at,
                ),
            )

    def events(self, task_id: str) -> tuple[MinecraftTaskEvent, ...]:
        with connect(self.config) as conn:
            rows = conn.execute(
                """
                SELECT event_id,task_id,event_type,payload,created_at
                FROM minecraft_task_events WHERE task_id=%s
                ORDER BY created_at,event_id
                """,
                (task_id,),
            ).fetchall()
        return tuple(
            MinecraftTaskEvent(
                event_id=row["event_id"],
                task_id=row["task_id"],
                event_type=row["event_type"],
                payload=row["payload"],
                created_at=row["created_at"].isoformat(),
            )
            for row in rows
        )

    def save_checkpoint(self, checkpoint: MinecraftCheckpoint) -> None:
        with connect(self.config) as conn:
            row = conn.execute(
                """
                INSERT INTO minecraft_checkpoints
                    (task_id,version,plan_version,payload,created_at)
                VALUES (%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT (task_id) DO UPDATE SET
                    version=EXCLUDED.version,
                    plan_version=EXCLUDED.plan_version,
                    payload=EXCLUDED.payload,
                    created_at=EXCLUDED.created_at
                WHERE minecraft_checkpoints.version <= EXCLUDED.version
                RETURNING task_id
                """,
                (
                    checkpoint.task_id,
                    checkpoint.version,
                    checkpoint.plan_version,
                    self._safe_json(checkpoint.payload),
                    checkpoint.created_at,
                ),
            ).fetchone()
            if row is None:
                raise StoreConflict("checkpoint version cannot move backwards")

    def latest_checkpoint(self, task_id: str) -> MinecraftCheckpoint | None:
        with connect(self.config) as conn:
            row = conn.execute(
                """
                SELECT task_id,version,plan_version,payload,created_at
                FROM minecraft_checkpoints WHERE task_id=%s
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return MinecraftCheckpoint(
            task_id=row["task_id"],
            version=row["version"],
            plan_version=row["plan_version"],
            payload=row["payload"],
            created_at=row["created_at"].isoformat(),
        )

    def list_recoverable(self) -> list[MinecraftTask]:
        with connect(self.config) as conn:
            rows = conn.execute(
                "SELECT task_json,cancel_requested FROM minecraft_tasks "
                "WHERE status=ANY(%s) ORDER BY created_at",
                (list(_ACTIVE_STATUSES),),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def active_for_bot(self, bot_id: str) -> MinecraftTask | None:
        with connect(self.config) as conn:
            row = conn.execute(
                "SELECT task_json,cancel_requested FROM minecraft_tasks "
                "WHERE bot_id=%s AND status=ANY(%s) LIMIT 1",
                (bot_id, list(_ACTIVE_STATUSES)),
            ).fetchone()
        return self._task_from_row(row) if row else None

    def request_cancel(self, task_id: str) -> MinecraftTask:
        with connect(self.config) as conn:
            row = conn.execute(
                """
                UPDATE minecraft_tasks SET
                    cancel_requested=TRUE,
                    version=version+1,
                    task_json=jsonb_set(
                        jsonb_set(task_json,'{cancel_requested}','true'::jsonb),
                        '{version}',to_jsonb(version+1)
                    ),
                    updated_at=NOW()
                WHERE task_id=%s
                RETURNING task_json,cancel_requested
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task_from_row(row)

    def acquire_lease(
        self, task_id: str, *, owner_id: str, ttl_seconds: float
    ) -> bool:
        with connect(self.config) as conn:
            row = conn.execute(
                """
                UPDATE minecraft_tasks
                SET lease_owner=%s,
                    lease_expires_at=NOW()+(%s * INTERVAL '1 second')
                WHERE task_id=%s
                  AND (lease_owner IS NULL OR lease_owner=%s OR lease_expires_at<=NOW())
                RETURNING task_id
                """,
                (owner_id, float(ttl_seconds), task_id, owner_id),
            ).fetchone()
        return row is not None

    def renew_lease(
        self, task_id: str, *, owner_id: str, ttl_seconds: float
    ) -> bool:
        with connect(self.config) as conn:
            row = conn.execute(
                """
                UPDATE minecraft_tasks
                SET lease_expires_at=NOW()+(%s * INTERVAL '1 second')
                WHERE task_id=%s AND lease_owner=%s
                RETURNING task_id
                """,
                (float(ttl_seconds), task_id, owner_id),
            ).fetchone()
        return row is not None

    def release_lease(self, task_id: str, *, owner_id: str) -> bool:
        with connect(self.config) as conn:
            row = conn.execute(
                """
                UPDATE minecraft_tasks SET lease_owner=NULL,lease_expires_at=NULL
                WHERE task_id=%s AND lease_owner=%s RETURNING task_id
                """,
                (task_id, owner_id),
            ).fetchone()
        return row is not None

    @staticmethod
    def _safe_json(payload: dict[str, Any]) -> str:
        rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(rendered.encode("utf-8")) > 256_000:
            raise ValueError("Minecraft JSON payload exceeds 256KB")
        forbidden = {"token", "password", "authorization", "secret"}
        if any(str(key).lower() in forbidden for key in payload):
            raise ValueError("sensitive field is forbidden in Minecraft payload")
        return rendered

    @classmethod
    def _task_payload(cls, task: MinecraftTask) -> str:
        return cls._safe_json(task.model_dump(mode="json"))

    @staticmethod
    def _task_from_row(row: Any) -> MinecraftTask:
        payload = dict(row["task_json"])
        payload["cancel_requested"] = bool(row["cancel_requested"])
        return MinecraftTask.model_validate(payload)
