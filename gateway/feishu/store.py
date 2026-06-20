from __future__ import annotations

import base64
import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime.db import connect, is_integrity_error, resolve_database_config, row_get, sql


class FeishuGatewayStore:
    """Persists Feishu callback dedupe, active conversations, and outbound messages."""

    def __init__(self, database_url: str | Path | None = None) -> None:
        self.config = resolve_database_config(
            database_url,
            env_names=("FEISHU_DATABASE_URL", "GATEWAY_DATABASE_URL", "DATABASE_URL"),
            purpose="feishu gateway store",
        )
        self._conn = connect(self.config)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                sql(self.config, """
                CREATE TABLE IF NOT EXISTS feishu_events (
                    event_id   TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
                """)
            )
            self._conn.execute(
                sql(self.config, """
                CREATE TABLE IF NOT EXISTS feishu_conversations (
                    external_chat_id TEXT PRIMARY KEY,
                    conversation_id  TEXT NOT NULL,
                    updated_at       TEXT NOT NULL
                )
                """)
            )
            self._conn.execute(
                sql(self.config, """
                CREATE TABLE IF NOT EXISTS feishu_outbox (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id       TEXT NOT NULL,
                    text          TEXT NOT NULL,
                    message_type  TEXT NOT NULL DEFAULT 'text',
                    document_path TEXT,
                    caption       TEXT,
                    status        TEXT NOT NULL DEFAULT 'pending',
                    attempts      INTEGER NOT NULL DEFAULT 0,
                    source        TEXT,
                    metadata      TEXT NOT NULL DEFAULT '{}',
                    error         TEXT,
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL,
                    sent_at       TEXT
                )
                """)
            )
            self._conn.execute(
                sql(self.config, """
                CREATE INDEX IF NOT EXISTS idx_feishu_outbox_status
                ON feishu_outbox(status, id)
                """)
            )
            self._conn.commit()

    def mark_event_seen(self, event_id: str) -> bool:
        cleaned = str(event_id or "").strip()
        if not cleaned:
            return True
        with self._lock:
            try:
                self._conn.execute(
                    sql(self.config, "INSERT INTO feishu_events (event_id, created_at) VALUES (?, ?)"),
                    (cleaned, datetime.now(timezone.utc).isoformat()),
                )
                self._conn.commit()
                return True
            except Exception as exc:
                if is_integrity_error(exc):
                    self._conn.rollback()
                    return False
                raise

    def get_conversation_id(self, external_chat_id: str) -> str:
        chat_id = str(external_chat_id)
        with self._lock:
            row = self._conn.execute(
                sql(self.config, """
                SELECT conversation_id
                FROM feishu_conversations
                WHERE external_chat_id = ?
                """),
                (chat_id,),
            ).fetchone()
            if row is not None:
                return str(row_get(row, "conversation_id", row_get(row, 0)))
            conversation_id = "default"
            self._save_conversation(chat_id, conversation_id)
        return conversation_id

    def start_conversation(self, external_chat_id: str) -> str:
        chat_id = str(external_chat_id)
        conversation_id = secrets.token_hex(6)
        with self._lock:
            self._save_conversation(chat_id, conversation_id)
        return conversation_id

    def runtime_chat_id(self, external_chat_id: str, *, user_id: str | None = None) -> str:
        return build_runtime_chat_id(
            external_chat_id,
            self.get_conversation_id(external_chat_id),
            user_id=user_id,
        )

    def enqueue_message(
        self,
        *,
        chat_id: str,
        text: str,
        source: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            query = """
                INSERT INTO feishu_outbox (
                    chat_id, text, message_type, status, attempts, source, metadata,
                    created_at, updated_at
                )
                VALUES (?, ?, 'text', 'pending', 0, ?, ?, ?, ?)
                RETURNING id
            """
            cursor = self._conn.execute(
                sql(self.config, query),
                (str(chat_id), str(text), str(source or ""), _json_dumps(metadata or {}), now, now),
            )
            inserted_id = _inserted_id(cursor)
            self._conn.commit()
            return inserted_id

    def enqueue_document(
        self,
        *,
        chat_id: str,
        document_path: str | Path,
        caption: str = "",
        source: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        path_text = str(document_path)
        caption_text = str(caption or "")
        with self._lock:
            query = """
                INSERT INTO feishu_outbox (
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
                FROM feishu_outbox
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
                UPDATE feishu_outbox
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
                sql(self.config, "SELECT attempts FROM feishu_outbox WHERE id = ?"),
                (int(message_id),),
            ).fetchone()
            attempts = int(row_get(row, "attempts", row_get(row, 0, 0))) + 1 if row is not None else 1
            status = "failed" if attempts >= max(1, int(max_attempts)) else "pending"
            self._conn.execute(
                sql(self.config, """
                UPDATE feishu_outbox
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
            INSERT INTO feishu_conversations (
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
    external_chat_id: str,
    conversation_id: str,
    *,
    user_id: str | None = None,
) -> str:
    suffix = f":{user_id}" if user_id else ""
    return f"fs:{_encode_id(external_chat_id)}:{conversation_id}{suffix}"


def external_chat_id_from_runtime(value: str) -> str:
    parts = str(value or "").split(":", 3)
    if len(parts) < 3 or parts[0] != "fs":
        raise ValueError("Invalid Feishu runtime chat ID.")
    return _decode_id(parts[1])


def _encode_id(value: str) -> str:
    return base64.urlsafe_b64encode(str(value).encode("utf-8")).decode("ascii").rstrip("=")


def _decode_id(value: str) -> str:
    padded = str(value) + "=" * (-len(str(value)) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_loads(value: str) -> dict[str, Any]:
    try:
        loaded = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _inserted_id(cursor) -> int:
    row = cursor.fetchone()
    return int(row_get(row, "id", row_get(row, 0)))
