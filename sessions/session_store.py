import json
import threading
from pathlib import Path
from typing import Any

from runtime.db import connect, execute_many, resolve_database_config, row_get, sql


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
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                sql(self.config, """
                CREATE TABLE IF NOT EXISTS sessions (
                    id             TEXT PRIMARY KEY,
                    current_mode   TEXT NOT NULL,
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
            self._conn.commit()

    def load_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                sql(self.config, """
                SELECT id, current_mode, created_at, updated_at, last_compacted, metadata
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

        return {
            "id": row_get(row, "id"),
            "current_mode": row_get(row, "current_mode"),
            "created_at": row_get(row, "created_at"),
            "updated_at": row_get(row, "updated_at"),
            "last_compacted": row_get(row, "last_compacted"),
            "metadata": json.loads(row_get(row, "metadata", "{}") or "{}"),
            "messages": [
                json.loads(row_get(msg_row, "message_json", "{}"))
                for msg_row in msg_rows
            ],
        }

    def save_session(self, session: Any) -> None:
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
        with self._lock:
            self._conn.execute(
                sql(self.config, """
                INSERT INTO sessions (
                    id, current_mode, created_at, updated_at, last_compacted, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    current_mode = excluded.current_mode,
                    updated_at = excluded.updated_at,
                    last_compacted = excluded.last_compacted,
                    metadata = excluded.metadata
                """),
                (
                    session.id,
                    session.current_mode,
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
            self._conn.commit()

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                sql(self.config, """
                SELECT id, current_mode, created_at, updated_at, last_compacted, metadata
                FROM sessions
                ORDER BY updated_at DESC
                """)
            ).fetchall()
        return [
            {
                "id": row_get(row, "id"),
                "current_mode": row_get(row, "current_mode"),
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
