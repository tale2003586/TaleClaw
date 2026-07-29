import json
import hashlib
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime.context.events import ContextEvent, ContextEventType, canonical_json, payload_checksum
from runtime.db import (
    connect,
    execute_many,
    resolve_database_config,
    row_get,
    sql,
    table_columns,
)


class SessionStore:
    def __init__(self, database_url: str | Path | None = None) -> None:
        self.config = resolve_database_config(
            database_url,
            env_names=("SESSION_DATABASE_URL", "DATABASE_URL"),
            purpose="session store",
        )
        self._conn = connect(self.config)
        self._lock = threading.Lock()
        self.last_message_insert_count = 0
        self.last_event_insert_count = 0
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn.transaction():
            self._conn.execute(
                sql(self.config, """
                CREATE TABLE IF NOT EXISTS sessions (
                    id             TEXT PRIMARY KEY,
                    active_agent   TEXT NOT NULL,
                    created_at     TEXT NOT NULL,
                    updated_at     TEXT NOT NULL,
                    last_compacted TEXT,
                    metadata       TEXT NOT NULL DEFAULT '{}'
                )
                """)
            )
            self._conn.execute(
                sql(self.config, """
                CREATE TABLE IF NOT EXISTS messages (
                    session_id   TEXT NOT NULL,
                    seq          INTEGER NOT NULL,
                    role         TEXT NOT NULL,
                    timestamp    TEXT,
                    message_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, seq),
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
                """)
            )
            self._conn.execute(
                sql(self.config, """
                CREATE TABLE IF NOT EXISTS context_events (
                    session_id     TEXT NOT NULL,
                    seq            BIGINT NOT NULL,
                    event_id       TEXT NOT NULL,
                    event_type     TEXT NOT NULL,
                    created_at     TEXT NOT NULL,
                    payload_json   TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    PRIMARY KEY (session_id, seq),
                    UNIQUE (event_id),
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
                """)
            )
            self._conn.execute(
                sql(self.config, """
                CREATE INDEX IF NOT EXISTS idx_context_events_session_seq
                ON context_events (session_id, seq)
                """)
            )
            self._conn.execute(
                sql(self.config, """
                CREATE TABLE IF NOT EXISTS context_checkpoints (
                    checkpoint_id       TEXT PRIMARY KEY,
                    session_id          TEXT NOT NULL,
                    archive_boundary_seq BIGINT NOT NULL,
                    completion_event_id TEXT NOT NULL,
                    created_at          TEXT NOT NULL,
                    state_json          TEXT NOT NULL,
                    state_sha256        TEXT NOT NULL,
                    metadata_json       TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
                """)
            )
            self._conn.execute(
                sql(self.config, """
                CREATE INDEX IF NOT EXISTS idx_context_checkpoints_session_boundary
                ON context_checkpoints (session_id, archive_boundary_seq DESC, created_at DESC)
                """)
            )
            self._migrate_agent_identity()

    def _migrate_agent_identity(self) -> None:
        """Migrate and remove the pre-Phase-7 session mode column."""
        columns = table_columns(self._conn, "sessions")
        legacy_column = "current" + "_" + "mode"
        if legacy_column not in columns:
            return

        if "active_agent" not in columns:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN active_agent TEXT"
            )
        self._conn.execute(
            f"""
            UPDATE sessions
            SET active_agent = COALESCE(
                NULLIF(active_agent, ''),
                NULLIF({legacy_column}, ''),
                'hybrid'
            )
            """
        )
        invalid_count = self._conn.execute(
            """
            SELECT COUNT(*) AS invalid_count
            FROM sessions
            WHERE active_agent IS NULL OR active_agent = ''
            """
        ).fetchone()
        if int(row_get(invalid_count, "invalid_count", row_get(invalid_count, 0, 0))) > 0:
            raise RuntimeError(
                "session store migration could not populate sessions.active_agent"
            )
        self._conn.execute(
            "ALTER TABLE sessions ALTER COLUMN active_agent SET NOT NULL"
        )
        self._conn.execute(
            f"ALTER TABLE sessions DROP COLUMN {legacy_column}"
        )

    def load_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                sql(self.config, """
                SELECT id, active_agent, created_at, updated_at, last_compacted, metadata
                FROM sessions
                WHERE id = ?
                """),
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            msg_rows = self._conn.execute(
                sql(self.config, """
                SELECT message_json
                FROM messages
                WHERE session_id = ?
                ORDER BY seq ASC
                """),
                (session_id,),
            ).fetchall()
            event_rows = self._conn.execute(
                sql(self.config, """
                SELECT session_id, seq, event_id, event_type, created_at, payload_json
                FROM context_events
                WHERE session_id = ?
                ORDER BY seq ASC
                """),
                (session_id,),
            ).fetchall()
            checkpoint_rows = self._conn.execute(
                sql(self.config, """
                SELECT checkpoint_id, archive_boundary_seq, completion_event_id,
                       created_at, state_json, state_sha256, metadata_json
                FROM context_checkpoints
                WHERE session_id = ?
                ORDER BY archive_boundary_seq DESC, created_at DESC
                """),
                (session_id,),
            ).fetchall()

        checkpoints = []
        for checkpoint in checkpoint_rows:
            state_json = str(row_get(checkpoint, "state_json", "{}") or "{}")
            expected_sha = str(row_get(checkpoint, "state_sha256") or "")
            actual_sha = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
            if expected_sha and actual_sha != expected_sha:
                continue
            checkpoints.append({
                "checkpoint_id": row_get(checkpoint, "checkpoint_id"),
                "archive_boundary_seq": int(row_get(checkpoint, "archive_boundary_seq", 0)),
                "completion_event_id": row_get(checkpoint, "completion_event_id"),
                "created_at": row_get(checkpoint, "created_at"),
                "state": json.loads(state_json),
                "state_sha256": expected_sha,
                "metadata": json.loads(row_get(checkpoint, "metadata_json", "{}") or "{}"),
            })

        return {
            "id": row_get(row, "id"),
            "active_agent": row_get(row, "active_agent"),
            "created_at": row_get(row, "created_at"),
            "updated_at": row_get(row, "updated_at"),
            "last_compacted": row_get(row, "last_compacted"),
            "metadata": json.loads(row_get(row, "metadata", "{}") or "{}"),
            "messages": [
                json.loads(row_get(msg_row, "message_json", "{}"))
                for msg_row in msg_rows
            ],
            "event_log": [
                ContextEvent.create(
                    event_id=str(row_get(event, "event_id")),
                    session_id=str(row_get(event, "session_id")),
                    seq=int(row_get(event, "seq")),
                    event_type=str(row_get(event, "event_type")),
                    created_at=str(row_get(event, "created_at")),
                    payload=json.loads(row_get(event, "payload_json", "{}") or "{}"),
                )
                for event in event_rows
            ],
            "archive_boundary_seq": checkpoints[0]["archive_boundary_seq"] if checkpoints else 0,
            "checkpoints": checkpoints,
        }

    def save_session(self, session: Any) -> None:
        if hasattr(session, "_backfill_legacy_messages"):
            session._backfill_legacy_messages()
        metadata_json = json.dumps(
            session.metadata or {},
            ensure_ascii=False,
            default=str,
        )
        message_rows = [
            (
                session.id,
                seq,
                str(message.get("role", "")),
                message.get("timestamp"),
                json.dumps(message, ensure_ascii=False, default=str),
            )
            for seq, message in enumerate(session.messages)
        ]
        with self._lock, self._conn.transaction():
            self._conn.execute(
                sql(self.config, """
                INSERT INTO sessions (
                    id, active_agent, created_at, updated_at, last_compacted, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    active_agent = excluded.active_agent,
                    updated_at = excluded.updated_at,
                    last_compacted = excluded.last_compacted,
                    metadata = excluded.metadata
                """),
                (
                    session.id,
                    session.active_agent,
                    session.created_at,
                    session.updated_at,
                    session.last_compacted,
                    metadata_json,
                ),
            )
            existing_rows = self._conn.execute(
                sql(self.config, """
                SELECT seq, message_json
                FROM messages
                WHERE session_id = ?
                ORDER BY seq ASC
                """),
                (session.id,),
            ).fetchall()
            existing_by_seq = {
                int(row_get(row, "seq")): row_get(row, "message_json", "")
                for row in existing_rows
            }
            first_changed = None
            for seq, row in enumerate(message_rows):
                if existing_by_seq.get(seq) != row[4]:
                    first_changed = seq
                    break
            if first_changed is None and any(
                seq >= len(message_rows) for seq in existing_by_seq
            ):
                first_changed = len(message_rows)

            rows_to_insert = []
            if first_changed is not None:
                self._conn.execute(
                    sql(self.config, """
                    DELETE FROM messages
                    WHERE session_id = ? AND seq >= ?
                    """),
                    (session.id, first_changed),
                )
                rows_to_insert = message_rows[first_changed:]

            execute_many(
                self._conn,
                self.config,
                """
                INSERT INTO messages (session_id, seq, role, timestamp, message_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows_to_insert,
            )
            self.last_message_insert_count = len(rows_to_insert)
            self.last_event_insert_count = self._append_new_events(session)
        # A preceding read can leave psycopg in an implicit transaction.  In
        # that case ``transaction()`` is a savepoint, so close the outer unit.
        self._conn.commit()

    def compact_session(
        self,
        session: Any,
        *,
        checkpoint: dict[str, Any],
        archive_boundary_seq: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a checkpoint, its completion fact, and its archive boundary atomically."""
        if not isinstance(checkpoint, dict):
            raise TypeError("checkpoint must be a dictionary")
        if hasattr(session, "_backfill_legacy_messages"):
            session._backfill_legacy_messages()
        requested_boundary = (
            session.event_log[-1].seq if archive_boundary_seq is None and session.event_log else 0
        ) if archive_boundary_seq is None else int(archive_boundary_seq)
        safe_boundary = session._safe_archive_boundary(max(0, requested_boundary))
        state_json = canonical_json(checkpoint)
        state_sha256 = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
        created_at = datetime.now(timezone.utc).isoformat()
        checkpoint_id = "ckpt_" + hashlib.sha256(
            canonical_json({
                "session_id": session.id,
                "archive_boundary_seq": safe_boundary,
                "state_sha256": state_sha256,
                "created_at": created_at,
            }).encode("utf-8")
        ).hexdigest()[:32]
        checkpoint_event = session.append_event(
            ContextEventType.TASK_STATE_CHECKPOINT,
            {
                "checkpoint_id": checkpoint_id,
                "archive_boundary_seq": safe_boundary,
                "state_sha256": state_sha256,
            },
            created_at=created_at,
        )
        completion_event = session.append_event(
            ContextEventType.COMPACTION_COMPLETED,
            {
                "checkpoint_id": checkpoint_id,
                "checkpoint_event_id": checkpoint_event.event_id,
                "archive_boundary_seq": safe_boundary,
                "state_sha256": state_sha256,
            },
            created_at=created_at,
        )
        metadata_json = canonical_json(metadata or {})
        previous_last_compacted = session.last_compacted
        session.last_compacted = created_at
        try:
            with self._lock, self._conn.transaction():
                self._upsert_session(session)
                self._save_messages(session)
                self.last_event_insert_count = self._append_new_events(session)
                self._conn.execute(
                    sql(self.config, """
                    INSERT INTO context_checkpoints (
                        checkpoint_id, session_id, archive_boundary_seq,
                        completion_event_id, created_at, state_json, state_sha256,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """),
                    (
                        checkpoint_id,
                        session.id,
                        safe_boundary,
                        completion_event.event_id,
                        created_at,
                        state_json,
                        state_sha256,
                        metadata_json,
                    ),
                )
            self._conn.commit()
        except Exception:
            # The in-memory facts did not reach durable storage, so make retry
            # deterministic by removing only the two events we created here.
            session.event_log = [
                event for event in session.event_log
                if event.event_id not in {checkpoint_event.event_id, completion_event.event_id}
            ]
            session._refresh_active_event_window()
            session.last_compacted = previous_last_compacted
            raise

        session.set_archive_boundary(safe_boundary)
        session.checkpoints.insert(0, {
            "checkpoint_id": checkpoint_id,
            "archive_boundary_seq": safe_boundary,
            "completion_event_id": completion_event.event_id,
            "created_at": created_at,
            "state": json.loads(state_json),
            "state_sha256": state_sha256,
            "metadata": json.loads(metadata_json),
        })
        return dict(session.checkpoints[0])

    # Naming aliases keep the storage boundary discoverable to callers during
    # the migration away from message-list compaction.
    save_compaction = compact_session
    persist_compaction = compact_session

    def _upsert_session(self, session: Any) -> None:
        metadata_json = json.dumps(session.metadata or {}, ensure_ascii=False, default=str)
        self._conn.execute(
            sql(self.config, """
            INSERT INTO sessions (
                id, active_agent, created_at, updated_at, last_compacted, metadata
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                active_agent = excluded.active_agent,
                updated_at = excluded.updated_at,
                last_compacted = excluded.last_compacted,
                metadata = excluded.metadata
            """),
            (
                session.id, session.active_agent, session.created_at,
                session.updated_at, session.last_compacted, metadata_json,
            ),
        )

    def _save_messages(self, session: Any) -> None:
        message_rows = [
            (
                session.id, seq, str(message.get("role", "")), message.get("timestamp"),
                json.dumps(message, ensure_ascii=False, default=str),
            )
            for seq, message in enumerate(session.messages)
        ]
        existing_rows = self._conn.execute(
            sql(self.config, """
            SELECT seq, message_json FROM messages WHERE session_id = ? ORDER BY seq ASC
            """),
            (session.id,),
        ).fetchall()
        existing_by_seq = {
            int(row_get(row, "seq")): row_get(row, "message_json", "") for row in existing_rows
        }
        first_changed = next(
            (seq for seq, row in enumerate(message_rows) if existing_by_seq.get(seq) != row[4]),
            None,
        )
        if first_changed is None and any(seq >= len(message_rows) for seq in existing_by_seq):
            first_changed = len(message_rows)
        rows_to_insert = []
        if first_changed is not None:
            self._conn.execute(
                sql(self.config, "DELETE FROM messages WHERE session_id = ? AND seq >= ?"),
                (session.id, first_changed),
            )
            rows_to_insert = message_rows[first_changed:]
        execute_many(
            self._conn, self.config,
            "INSERT INTO messages (session_id, seq, role, timestamp, message_json) VALUES (?, ?, ?, ?, ?)",
            rows_to_insert,
        )
        self.last_message_insert_count = len(rows_to_insert)

    def _append_new_events(self, session: Any) -> int:
        inserted = 0
        for event in getattr(session, "event_log", []):
            payload_json = canonical_json(event.payload)
            cursor = self._conn.execute(
                sql(self.config, """
                INSERT INTO context_events (
                    session_id, seq, event_id, event_type, created_at,
                    payload_json, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (session_id, seq) DO NOTHING
                """),
                (
                    event.session_id, event.seq, event.event_id, event.type,
                    event.created_at, payload_json, payload_checksum(event.payload),
                ),
            )
            inserted += max(0, int(cursor.rowcount or 0))
        return inserted

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                sql(self.config, """
                SELECT id, active_agent, created_at, updated_at, last_compacted, metadata
                FROM sessions
                ORDER BY updated_at DESC
                """)
            ).fetchall()
        return [
            {
                "id": row_get(row, "id"),
                "active_agent": row_get(row, "active_agent"),
                "created_at": row_get(row, "created_at"),
                "updated_at": row_get(row, "updated_at"),
                "last_compacted": row_get(row, "last_compacted"),
                "metadata": json.loads(row_get(row, "metadata", "{}") or "{}"),
            }
            for row in rows
        ]

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            self._conn.execute(
                sql(self.config, "DELETE FROM context_checkpoints WHERE session_id = ?"),
                (session_id,),
            )
            self._conn.execute(
                sql(self.config, "DELETE FROM context_events WHERE session_id = ?"),
                (session_id,),
            )
            self._conn.execute(
                sql(self.config, "DELETE FROM messages WHERE session_id = ?"),
                (session_id,),
            )
            cursor = self._conn.execute(
                sql(self.config, "DELETE FROM sessions WHERE id = ?"),
                (session_id,),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
