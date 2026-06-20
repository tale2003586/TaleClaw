from __future__ import annotations

import re
from pathlib import Path


DEFAULT_USER_ID = "local"
DEFAULT_USER_ROLE = "admin"
USER_ROLE_VALUES = {"admin", "user"}
_USER_ID_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")
_CHAT_ID_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")


def normalize_user_id(value: str | None) -> str:
    user_id = str(value or "").strip()
    if not _USER_ID_PATTERN.fullmatch(user_id):
        raise ValueError(
            "User ID must start with a letter or number and contain only "
            "letters, numbers, dots, underscores, or hyphens."
        )
    return user_id


def normalize_user_role(value: str | None) -> str:
    role = str(value or DEFAULT_USER_ROLE).strip().lower()
    if role not in USER_ROLE_VALUES:
        raise ValueError(f"Unknown user role: {role}")
    return role


def explicit_user_id_for_session(session) -> str | None:
    metadata = getattr(session, "metadata", {}) or {}
    value = metadata.get("user_id")
    if value is None:
        return None
    return normalize_user_id(str(value))


def user_id_for_session(session) -> str:
    return explicit_user_id_for_session(session) or DEFAULT_USER_ID


def user_role_for_session(session) -> str:
    metadata = getattr(session, "metadata", {}) or {}
    return normalize_user_role(metadata.get("user_role"))


def user_data_root(workspace: str | Path, user_id: str) -> Path:
    workspace_root = Path(workspace).resolve()
    root = (workspace_root / ".users" / normalize_user_id(user_id)).resolve()
    users_root = (workspace_root / ".users").resolve()
    if root != users_root and not root.is_relative_to(users_root):
        raise ValueError("User data directory escapes .users.")
    return root


def storage_root_for_user(workspace: str | Path, user_id: str) -> Path:
    return user_data_root(workspace, user_id) / "storage"


def memory_root_for_user(workspace: str | Path, user_id: str) -> Path:
    return user_data_root(workspace, user_id) / "memory"


def storage_root_for_session(workspace: str | Path, session) -> Path:
    user_id = explicit_user_id_for_session(session)
    if user_id is None:
        return (Path(workspace).resolve() / "storage").resolve()
    return storage_root_for_user(workspace, user_id).resolve()


def memory_root_for_session(workspace: str | Path, session) -> Path:
    user_id = explicit_user_id_for_session(session)
    if user_id is None:
        return (Path(workspace).resolve() / "memory").resolve()
    return memory_root_for_user(workspace, user_id).resolve()


def normalize_chat_id(value: str | None) -> str:
    chat_id = str(value or "").strip()
    if not _CHAT_ID_PATTERN.fullmatch(chat_id):
        raise ValueError(
            "Session ID must start with a letter or number and contain only "
            "letters, numbers, dots, underscores, or hyphens."
        )
    return chat_id


def web_chat_id(user_id: str, chat_id: str) -> str:
    return f"{normalize_user_id(user_id)}:{normalize_chat_id(chat_id)}"


def web_session_id(user_id: str, chat_id: str) -> str:
    return f"web:{web_chat_id(user_id, chat_id)}"


def parse_web_session_id(value: str) -> tuple[str, str] | None:
    parts = str(value or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "web":
        return None
    try:
        return normalize_user_id(parts[1]), normalize_chat_id(parts[2])
    except ValueError:
        return None
