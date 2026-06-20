from __future__ import annotations

import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime.db import connect, resolve_database_config, row_get, sql


class TelegramGatewayStore:
    """Persists long-polling progress and per-chat active conversations."""

    def __init__(self, database_url: str | Path | None = None) -> None:
        self.config = resolve_database_config(
            database_url,
            env_names=("TELEGRAM_DATABASE_URL", "GATEWAY_DATABASE_URL", "DATABASE_URL"),
            purpose="telegram gateway store",
        )
        self._conn = connect(self.config)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                sql(self.config, """
                CREATE TABLE IF NOT EXISTS telegram_state (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """)
            )
            self._conn.execute(
                sql(self.config, """
                CREATE TABLE IF NOT EXISTS telegram_conversations (
                    external_chat_id TEXT PRIMARY KEY,
                    conversation_id  TEXT NOT NULL,
                    updated_at       TEXT NOT NULL
                )
                """)
            )
            self._conn.execute(
                sql(self.config, """
                CREATE TABLE IF NOT EXISTS telegram_outbox (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id    TEXT NOT NULL,
                    text       TEXT NOT NULL,
                    message_type TEXT NOT NULL DEFAULT 'text',
                    document_path TEXT,
                    caption    TEXT,
                    status     TEXT NOT NULL DEFAULT 'pending',
                    attempts   INTEGER NOT NULL DEFAULT 0,
                    source     TEXT,
                    metadata   TEXT NOT NULL DEFAULT '{}',
                    error      TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sent_at    TEXT
                )
                """)
            )
            self._ensure_column(
                "telegram_outbox",
                "message_type",
                "TEXT NOT NULL DEFAULT 'text'",
            )
            self._ensure_column("telegram_outbox", "document_path", "TEXT")
            self._ensure_column("telegram_outbox", "caption", "TEXT")
            self._conn.execute(
                sql(self.config, """
                CREATE INDEX IF NOT EXISTS idx_telegram_outbox_status
                ON telegram_outbox(status, id)
                """)
            )
            self._conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self._conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = %s
            """,
            (table,),
        ).fetchall()
        columns = {str(row_get(row, "column_name", "")) for row in rows}
        if column not in columns:
            self._conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    def get_offset(self) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM telegram_state WHERE key = 'offset'"
            ).fetchone()
        return int(row_get(row, "value", row_get(row, 0))) if row is not None else None

    def set_offset(self, offset: int) -> None:
        with self._lock:
            self._conn.execute(
                sql(self.config, """
                INSERT INTO telegram_state (key, value)
                VALUES ('offset', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """),
                (str(int(offset)),),
            )
            self._conn.commit()

    def get_conversation_id(self, external_chat_id: int | str) -> str:
        chat_id = str(external_chat_id)
        with self._lock:
            row = self._conn.execute(
                sql(self.config, """
                SELECT conversation_id
                FROM telegram_conversations
                WHERE external_chat_id = ?
                """),
                (chat_id,),
            ).fetchone()
            if row is not None:
                return str(row_get(row, "conversation_id", row_get(row, 0)))
            conversation_id = "default"
            self._save_conversation(chat_id, conversation_id)
        return conversation_id

    def start_conversation(self, external_chat_id: int | str) -> str:
        chat_id = str(external_chat_id)
        conversation_id = secrets.token_hex(6)
        with self._lock:
            self._save_conversation(chat_id, conversation_id)
        return conversation_id

    def runtime_chat_id(
        self,
        external_chat_id: int | str,
        *,
        user_id: str | None = None,
    ) -> str:
        return build_runtime_chat_id(
            external_chat_id,
            self.get_conversation_id(external_chat_id),
            user_id=user_id,
        )

    def enqueue_message(
        self,
        *,
        chat_id: int | str,
        text: str,
        source: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            query = """
                INSERT INTO telegram_outbox (
                    chat_id, text, message_type, status, attempts, source, metadata,
                    created_at, updated_at
                )
                VALUES (?, ?, 'text', 'pending', 0, ?, ?, ?, ?)
                RETURNING id
            """
            cursor = self._conn.execute(
                sql(self.config, query),
                (
                    str(chat_id),
                    str(text),
                    str(source or ""),
                    _json_dumps(metadata or {}),
                    now,
                    now,
                ),
            )
            inserted_id = _inserted_id(cursor)
            self._conn.commit()
            return inserted_id

    def enqueue_document(
        self,
        *,
        chat_id: int | str,
        document_path: str | Path,
        caption: str = "",
        source: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        caption_text = str(caption or "")
        path_text = str(document_path)
        with self._lock:
            query = """
                INSERT INTO telegram_outbox (
                    chat_id, text, message_type, document_path, caption, status,
                    attempts, source, metadata, created_at, updated_at
                )
                VALUES (?, ?, 'document', ?, ?, 'pending', 0, ?, ?, ?, ?)
                RETURNING id
            """
            cursor = self._conn.execute(
                sql(self.config, query),
                (
                    str(chat_id),
                    caption_text or path_text,
                    path_text,
                    caption_text,
                    str(source or ""),
                    _json_dumps(metadata or {}),
                    now,
                    now,
                ),
            )
            inserted_id = _inserted_id(cursor)
            self._conn.commit()
            return inserted_id

    def list_pending_messages(self, *, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                sql(self.config, """
                SELECT id, chat_id, text, message_type, document_path, caption,
                       attempts, source, metadata, created_at
                FROM telegram_outbox
                WHERE status = 'pending'
                ORDER BY id ASC
                LIMIT ?
                """),
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "id": int(row_get(row, "id", row_get(row, 0))),
                "chat_id": row_get(row, "chat_id", row_get(row, 1)),
                "text": row_get(row, "text", row_get(row, 2)),
                "message_type": row_get(row, "message_type", row_get(row, 3)) or "text",
                "document_path": row_get(row, "document_path", row_get(row, 4)) or "",
                "caption": row_get(row, "caption", row_get(row, 5)) or "",
                "attempts": int(row_get(row, "attempts", row_get(row, 6))),
                "source": row_get(row, "source", row_get(row, 7)) or "",
                "metadata": _json_loads(row_get(row, "metadata", row_get(row, 8))),
                "created_at": row_get(row, "created_at", row_get(row, 9)),
            }
            for row in rows
        ]

    def mark_message_sent(self, message_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                sql(self.config, """
                UPDATE telegram_outbox
                SET status = 'sent',
                    updated_at = ?,
                    sent_at = ?
                WHERE id = ?
                """),
                (now, now, int(message_id)),
            )
            self._conn.commit()

    def mark_message_failed(
        self,
        message_id: int,
        *,
        error: str,
        max_attempts: int = 3,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            row = self._conn.execute(
                sql(self.config, "SELECT attempts FROM telegram_outbox WHERE id = ?"),
                (int(message_id),),
            ).fetchone()
            attempts = int(row_get(row, "attempts", row_get(row, 0, 0))) + 1 if row is not None else 1
            status = "failed" if attempts >= max(1, int(max_attempts)) else "pending"
            self._conn.execute(
                sql(self.config, """
                UPDATE telegram_outbox
                SET status = ?,
                    attempts = ?,
                    error = ?,
                    updated_at = ?
                WHERE id = ?
                """),
                (status, attempts, str(error)[:1000], now, int(message_id)),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _save_conversation(self, chat_id: str, conversation_id: str) -> None:
        self._conn.execute(
            sql(self.config, """
            INSERT INTO telegram_conversations (
                external_chat_id, conversation_id, updated_at
            )
            VALUES (?, ?, ?)
            ON CONFLICT(external_chat_id) DO UPDATE SET
                conversation_id = excluded.conversation_id,
                updated_at = excluded.updated_at
            """),
            (chat_id, conversation_id, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()


def build_runtime_chat_id(
    external_chat_id: int | str,
    conversation_id: str,
    *,
    user_id: str | None = None,
) -> str:
    suffix = f"_{user_id}" if user_id else ""
    return f"tg_{int(external_chat_id)}_{conversation_id}{suffix}"


def external_chat_id_from_runtime(value: str) -> int:
    parts = str(value or "").split("_", 2)
    if len(parts) != 3 or parts[0] != "tg":
        raise ValueError("Invalid Telegram runtime chat ID.")
    return int(parts[1])


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def _json_loads(value: str) -> dict[str, Any]:
    import json

    try:
        loaded = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _inserted_id(cursor) -> int:
    row = cursor.fetchone()
    return int(row_get(row, "id", row_get(row, 0)))
