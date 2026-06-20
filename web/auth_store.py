from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from runtime.db import connect, is_integrity_error, resolve_database_config, sql
from user_scope import normalize_user_id, normalize_user_role


PBKDF2_ITERATIONS = 310_000
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    role: str


@dataclass(frozen=True)
class EnvironmentUser:
    user_id: str
    password: str
    role: str


class WebAuthStore:
    """Web users and opaque browser sessions."""

    def __init__(self, database_url: str | Path | None = None) -> None:
        self.config = resolve_database_config(
            database_url,
            env_names=("WEB_AUTH_DATABASE_URL", "DATABASE_URL"),
            purpose="web auth store",
        )
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self):
        return connect(self.config)

    def _init_schema(self) -> None:
        with self._lock, closing(self._connect()) as conn:
            conn.execute(
                sql(self.config, """
                CREATE TABLE IF NOT EXISTS web_users (
                    user_id       TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    salt          TEXT NOT NULL,
                    role          TEXT NOT NULL,
                    source        TEXT NOT NULL,
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL
                )
                """)
            )
            conn.execute(
                sql(self.config, """
                CREATE TABLE IF NOT EXISTS web_auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id    TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES web_users(user_id)
                        ON DELETE CASCADE
                )
                """)
            )
            conn.execute(
                sql(self.config, """
                CREATE INDEX IF NOT EXISTS idx_web_auth_sessions_expiry
                ON web_auth_sessions(expires_at)
                """)
            )
            conn.commit()

    def sync_environment_users(
        self,
        users: Mapping[str, EnvironmentUser],
    ) -> None:
        for user in users.values():
            user_id = normalize_user_id(user.user_id)
            role = normalize_user_role(user.role)
            password = _validated_password(user.password)
            with self._lock, closing(self._connect()) as conn:
                existing = conn.execute(
                    sql(self.config, """
                    SELECT password_hash, salt, role, source
                    FROM web_users
                    WHERE user_id = ?
                    """),
                    (user_id,),
                ).fetchone()
                if (
                    existing is not None
                    and existing["source"] == "environment"
                    and existing["role"] == role
                    and _verify_password(
                        password,
                        salt=existing["salt"],
                        password_hash=existing["password_hash"],
                    )
                ):
                    continue
                salt, password_hash = _hash_password(password)
                now = _now_iso()
                conn.execute(
                    sql(self.config, """
                    INSERT INTO web_users (
                        user_id, password_hash, salt, role, source, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'environment', ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        password_hash = excluded.password_hash,
                        salt = excluded.salt,
                        role = excluded.role,
                        source = 'environment',
                        updated_at = excluded.updated_at
                    """),
                    (user_id, password_hash, salt, role, now, now),
                )
                conn.commit()

    def register(self, *, user_id: str, password: str) -> AuthenticatedUser:
        normalized_user_id = normalize_user_id(user_id)
        validated_password = _validated_password(password)
        salt, password_hash = _hash_password(validated_password)
        now = _now_iso()
        with self._lock, closing(self._connect()) as conn:
            try:
                conn.execute(
                    sql(self.config, """
                    INSERT INTO web_users (
                        user_id, password_hash, salt, role, source, created_at, updated_at
                    ) VALUES (?, ?, ?, 'user', 'registration', ?, ?)
                    """),
                    (normalized_user_id, password_hash, salt, now, now),
                )
                conn.commit()
            except Exception as exc:
                if is_integrity_error(exc):
                    conn.rollback()
                    raise ValueError("This username is already registered.") from exc
                raise
        return AuthenticatedUser(user_id=normalized_user_id, role="user")

    def authenticate(
        self,
        *,
        user_id: str,
        password: str,
    ) -> AuthenticatedUser | None:
        try:
            normalized_user_id = normalize_user_id(user_id)
        except ValueError:
            return None
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                sql(self.config, """
                SELECT user_id, password_hash, salt, role
                FROM web_users
                WHERE user_id = ?
                """),
                (normalized_user_id,),
            ).fetchone()
        if row is None:
            return None
        if not _verify_password(
            str(password or ""),
            salt=row["salt"],
            password_hash=row["password_hash"],
        ):
            return None
        return AuthenticatedUser(
            user_id=row["user_id"],
            role=normalize_user_role(row["role"]),
        )

    def create_session(
        self,
        user: AuthenticatedUser,
        *,
        ttl_hours: int = 168,
    ) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = _token_hash(token)
        now = _now()
        expires_at = now + timedelta(hours=max(1, int(ttl_hours)))
        with self._lock, closing(self._connect()) as conn:
            conn.execute(
                sql(self.config, "DELETE FROM web_auth_sessions WHERE expires_at <= ?"),
                (now.isoformat(),),
            )
            conn.execute(
                sql(self.config, """
                INSERT INTO web_auth_sessions (
                    token_hash, user_id, created_at, expires_at
                ) VALUES (?, ?, ?, ?)
                """),
                (token_hash, user.user_id, now.isoformat(), expires_at.isoformat()),
            )
            conn.commit()
        return token

    def authenticate_session(self, token: str | None) -> AuthenticatedUser | None:
        if not token:
            return None
        token_hash = _token_hash(token)
        now = _now_iso()
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                sql(self.config, """
                SELECT u.user_id, u.role, s.expires_at
                FROM web_auth_sessions AS s
                JOIN web_users AS u ON u.user_id = s.user_id
                WHERE s.token_hash = ?
                """),
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now:
                conn.execute(
                    sql(self.config, "DELETE FROM web_auth_sessions WHERE token_hash = ?"),
                    (token_hash,),
                )
                conn.commit()
                return None
        return AuthenticatedUser(
            user_id=row["user_id"],
            role=normalize_user_role(row["role"]),
        )

    def revoke_session(self, token: str | None) -> None:
        if not token:
            return
        with self._lock, closing(self._connect()) as conn:
            conn.execute(
                sql(self.config, "DELETE FROM web_auth_sessions WHERE token_hash = ?"),
                (_token_hash(token),),
            )
            conn.commit()


def _validated_password(value: str) -> str:
    password = str(value or "")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must contain at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"Password must not exceed {MAX_PASSWORD_LENGTH} characters.")
    return password


def _hash_password(password: str) -> tuple[str, str]:
    salt = secrets.token_hex(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        PBKDF2_ITERATIONS,
    ).hex()
    return salt, password_hash


def _verify_password(password: str, *, salt: str, password_hash: str) -> bool:
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        PBKDF2_ITERATIONS,
    ).hex()
    return hmac.compare_digest(actual, password_hash)


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()
