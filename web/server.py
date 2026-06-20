from __future__ import annotations

"""Stdlib web console server.

For production deployment, run this behind a TLS-terminating reverse proxy
such as nginx/Caddy and restrict direct access to the Python process.
"""

import argparse
import asyncio
import base64
import hmac
import json
import mimetypes
import os
import queue
import shutil
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from html import escape as html_escape
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "web" / "static"
USERS_DIR = ROOT / ".users"
RUNS_DIR = ROOT / ".runs"
MEMORY_FILES = [
    "SELF.md",
    "MEMORY.md",
    "NOW.md",
    "PENDING.md",
    "RECENT_CONTEXT.md",
    "HISTORY.md",
]
DEFAULT_MAX_BODY_BYTES = 52_428_800
MAX_PREVIEW_BYTES = 1_000_000
AUTH_COOKIE_NAME = "taleclaw_session"
_CHAT_MARKDOWN = None

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from user_scope import (
    DEFAULT_USER_ID,
    DEFAULT_USER_ROLE,
    memory_root_for_user,
    normalize_user_id,
    normalize_user_role,
    parse_web_session_id,
    storage_root_for_user,
    web_chat_id,
    web_session_id,
)
from web.auth_store import AuthenticatedUser, EnvironmentUser, WebAuthStore


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_env_file(ROOT / ".env")
MAX_BODY_BYTES = int(os.environ.get("WEB_MAX_BODY_BYTES", str(DEFAULT_MAX_BODY_BYTES)))
USERS_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class UploadedFile:
    field_name: str
    filename: str
    content: bytes


def _parse_multipart_form(headers, body: bytes) -> tuple[dict[str, list[str]], list[UploadedFile]]:
    content_type = headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type.lower():
        raise ValueError("Content-Type must be multipart/form-data.")
    raw = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n"
        "\r\n"
    ).encode("utf-8") + body
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if not message.is_multipart():
        raise ValueError("Invalid multipart upload.")

    fields: dict[str, list[str]] = {}
    files: list[UploadedFile] = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files.append(
                UploadedFile(
                    field_name=str(name),
                    filename=Path(filename).name,
                    content=payload,
                )
            )
        else:
            charset = part.get_content_charset() or "utf-8"
            fields.setdefault(str(name), []).append(
                payload.decode(charset, errors="replace")
            )
    return fields, files


class AgentService:
    """Owns the async agent runtime behind the synchronous stdlib HTTP server."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runtime = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()
        self._start_error: BaseException | None = None
        self._session_locks: dict[str, asyncio.Lock] | None = None
        self._pending: dict[str, asyncio.Future[str]] = {}

    def ensure_started(self) -> None:
        with self._start_lock:
            if self._runtime is not None:
                return
            if self._start_error is not None:
                raise RuntimeError(_friendly_runtime_error(self._start_error)) from self._start_error
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run_loop,
                    name="agent-web-runtime",
                    daemon=True,
                )
                self._thread.start()

        startup_timeout = _env_int("AGENT_RUNTIME_STARTUP_TIMEOUT_SECONDS", 15)
        if not self._ready.wait(timeout=startup_timeout):
            raise RuntimeError(
                f"Agent runtime did not start within {startup_timeout} seconds."
            )
        if self._start_error is not None:
            raise RuntimeError(_friendly_runtime_error(self._start_error)) from self._start_error

    def ask(
        self,
        *,
        session_id: str,
        content: str,
        user_id: str = DEFAULT_USER_ID,
        user_role: str = DEFAULT_USER_ROLE,
        workspace_root: str | None = None,
        timeout: int = 180,
    ) -> str:
        return self.ask_stream(
            session_id=session_id,
            content=content,
            user_id=user_id,
            user_role=user_role,
            workspace_root=workspace_root,
            timeout=timeout,
        )

    def ask_stream(
        self,
        *,
        session_id: str,
        content: str,
        user_id: str = DEFAULT_USER_ID,
        user_role: str = DEFAULT_USER_ROLE,
        workspace_root: str | None = None,
        on_text: Callable[[str], None] | None = None,
        timeout: int = 1800,
    ) -> str:
        self.ensure_started()
        if self._loop is None:
            raise RuntimeError("Agent runtime loop is not available.")

        future = asyncio.run_coroutine_threadsafe(
            self._ask_async(
                session_id=session_id,
                content=content,
                user_id=user_id,
                user_role=user_role,
                workspace_root=workspace_root,
                on_text=on_text,
                reply_timeout=_env_int("WEB_AGENT_REPLY_TIMEOUT_SECONDS", timeout),
            ),
            self._loop,
        )
        return future.result(timeout=timeout)

    def delete_session(self, session_id: str, timeout: int = 10) -> bool:
        if self._thread is None and self._runtime is None:
            return _delete_stored_session(session_id)

        self.ensure_started()
        if self._loop is None:
            raise RuntimeError("Agent runtime loop is not available.")
        future = asyncio.run_coroutine_threadsafe(
            self._delete_session_async(session_id),
            self._loop,
        )
        return future.result(timeout=timeout)

    def request_cancel(self, storage_session_id: str, timeout: int = 10) -> bool:
        self.ensure_started()
        if self._runtime is None:
            raise RuntimeError("Agent runtime is not started.")
        request_cancel = getattr(self._runtime, "request_cancel", None)
        if request_cancel is None:
            return False
        return bool(request_cancel(storage_session_id))

    def stop(self) -> None:
        if self._loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._stop_async(), self._loop)
        try:
            future.result(timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._start_async())
        except BaseException as exc:
            self._start_error = exc
        finally:
            self._ready.set()

        if self._start_error is None:
            loop.run_forever()
        loop.close()

    async def _start_async(self) -> None:
        from runtime.bootstrap import build_runtime

        self._runtime = build_runtime()
        self._session_locks = {}
        self._runtime.bus.subscribe_outbound("web", self._handle_outbound)
        self._runtime.start()

    async def _stop_async(self) -> None:
        if self._runtime is None:
            return
        await self._runtime.stop()
        sessions = getattr(getattr(self._runtime, "loop", None), "sessions", None)
        if sessions is not None:
            sessions.close()

    async def _handle_outbound(self, message) -> None:
        reply_future = self._pending.pop(message.chat_id, None)
        if reply_future is not None and not reply_future.done():
            reply_future.set_result(message.content)

    async def _ask_async(
        self,
        *,
        session_id: str,
        content: str,
        user_id: str,
        user_role: str,
        workspace_root: str | None = None,
        on_text: Callable[[str], None] | None = None,
        reply_timeout: int = 180,
    ) -> str:
        if self._runtime is None or self._session_locks is None or self._loop is None:
            raise RuntimeError("Agent runtime is not started.")

        scoped_chat_id = web_chat_id(user_id, session_id)
        session_key = f"web:{scoped_chat_id}"
        async with self._lock_for_session(session_key):
            reply_future: asyncio.Future[str] = self._loop.create_future()
            self._pending[scoped_chat_id] = reply_future
            metadata = {
                "user_id": normalize_user_id(user_id),
                "user_role": normalize_user_role(user_role),
            }
            if workspace_root:
                metadata["workspace_root"] = str(workspace_root)
            try:
                await self._runtime.run_message(
                    content=content,
                    channel="web",
                    chat_id=scoped_chat_id,
                    metadata=metadata,
                    on_text=on_text,
                )
                return await asyncio.wait_for(
                    reply_future,
                    timeout=max(1, int(reply_timeout)),
                )
            finally:
                self._pending.pop(scoped_chat_id, None)

    async def _delete_session_async(self, session_id: str) -> bool:
        if self._runtime is None or self._session_locks is None:
            raise RuntimeError("Agent runtime is not started.")

        async with self._lock_for_session(session_id):
            sessions = getattr(getattr(self._runtime, "loop", None), "sessions", None)
            if sessions is None:
                raise RuntimeError("Agent session manager is not available.")
            return sessions.delete(session_id)

    def _lock_for_session(self, session_key: str) -> asyncio.Lock:
        if self._session_locks is None:
            raise RuntimeError("Agent runtime is not started.")
        lock = self._session_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_key] = lock
        return lock


def _friendly_runtime_error(exc: BaseException) -> str:
    if isinstance(exc, KeyError) and exc.args and exc.args[0] == "DEEPSEEK_API_KEY":
        return "缺少 DEEPSEEK_API_KEY。请在环境变量或 .env 中配置后再使用聊天功能。"
    if isinstance(exc, ModuleNotFoundError):
        return f"缺少 Python 模块：{exc.name}。现有 CLI 依赖需要先安装完整。"
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()


def read_sessions(user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
    from sessions.session_store import SessionStore

    store = SessionStore()
    try:
        rows = store.list_sessions()
    finally:
        store.close()

    sessions: list[dict[str, Any]] = []
    for row in rows:
        if _is_internal_task_session(row):
            continue
        parsed = parse_web_session_id(row["id"])
        if parsed is None or parsed[0] != user_id:
            continue
        _, chat_id = parsed
        sessions.append({
            **row,
            "channel": "web",
            "chat_id": chat_id,
            "can_chat": True,
        })
    return sessions


def _delete_stored_session(session_id: str) -> bool:
    from sessions.session_store import SessionStore

    store = SessionStore()
    try:
        return store.delete_session(session_id)
    finally:
        store.close()


def _web_storage_id(session_id: str, user_id: str = DEFAULT_USER_ID) -> str:
    value = str(session_id or "").strip()
    if not value:
        raise ValueError("session_id is required")

    parsed = parse_web_session_id(value)
    if parsed is not None:
        stored_user_id, chat_id = parsed
        if stored_user_id != user_id:
            raise ValueError("Session does not belong to the current user.")
        return value
    if ":" in value:
        raise ValueError("Only Web sessions owned by the current user can be accessed.")
    return web_session_id(user_id, value)


def _is_internal_task_session(row: dict[str, Any]) -> bool:
    metadata = row.get("metadata") or {}
    return row.get("id", "").startswith("task:") or metadata.get("kind") == "task_session"


def read_session(
    session_id: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    raw: bool = False,
) -> dict[str, Any]:
    from sessions.session_store import SessionStore

    storage_id = _web_storage_id(session_id, user_id)
    store = SessionStore()
    try:
        session = store.load_session(storage_id)
    finally:
        store.close()

    if session is None:
        _, chat_id = parse_web_session_id(storage_id) or (user_id, session_id)
        return {
            "id": storage_id,
            "chat_id": chat_id,
            "channel": "web",
            "can_chat": True,
            "messages": [],
            "current_mode": "hybrid",
        }
    _, chat_id = parse_web_session_id(session["id"]) or (user_id, session_id)
    session["channel"] = "web"
    session["chat_id"] = chat_id
    session["can_chat"] = True
    session["messages"] = [_web_message(message) for message in session["messages"]]
    return session


def _web_message(message: dict[str, Any]) -> dict[str, Any]:
    result = dict(message)
    content = result.get("content")
    if result.get("role") == "assistant" and isinstance(content, str):
        result["display_html"] = render_chat_markdown(content)
    return result


def render_chat_markdown(text: str) -> str:
    global _CHAT_MARKDOWN

    try:
        import mistune
    except ImportError:
        return f"<p>{html_escape(text).replace(chr(10), '<br>')}</p>"

    if _CHAT_MARKDOWN is None:
        class SafeChatRenderer(mistune.HTMLRenderer):
            def link(self, text: str, url: str, title: str | None = None) -> str:
                if urlparse(url).scheme not in {"http", "https", "mailto"}:
                    return text
                return super().link(text, url, title)

            def image(self, text: str, url: str, title: str | None = None) -> str:
                return html_escape(f"[image: {text or 'attachment'}]")

        _CHAT_MARKDOWN = mistune.create_markdown(
            renderer=SafeChatRenderer(escape=True),
            plugins=["table"],
        )
    return _CHAT_MARKDOWN(text)


def read_memory_files(user_id: str = DEFAULT_USER_ID) -> list[dict[str, str]]:
    memory_dir = memory_root_for_user(ROOT, user_id)
    files = []
    for name in MEMORY_FILES:
        path = memory_dir / name
        if path.exists():
            content = path.read_text(encoding="utf-8")
        else:
            content = ""
        files.append({"name": name, "content": content})
    return files


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _storage_dir(user_id: str) -> Path:
    root = storage_root_for_user(ROOT, user_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _analysis_record_path(user_id: str) -> Path:
    return _storage_dir(user_id) / "records" / "analysis.txt"


def _safe_storage_path(
    relative_path: str | None = "",
    *,
    user_id: str = DEFAULT_USER_ID,
) -> Path:
    storage_dir = _storage_dir(user_id)
    raw = unquote(str(relative_path or "")).strip().lstrip("/")
    candidate = (storage_dir / raw).resolve()
    if candidate != storage_dir and not candidate.is_relative_to(storage_dir):
        raise ValueError("Path escapes storage.")
    return candidate


def _storage_rel(path: Path, *, user_id: str = DEFAULT_USER_ID) -> str:
    storage_dir = _storage_dir(user_id)
    if path == storage_dir:
        return ""
    return path.relative_to(storage_dir).as_posix()


def _entry_for(path: Path, *, user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
    stat = path.stat()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    is_dir = path.is_dir()
    return {
        "name": path.name,
        "path": _storage_rel(path, user_id=user_id),
        "is_dir": is_dir,
        "size": 0 if is_dir else stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "mime": "inode/directory" if is_dir else mime,
        "previewable": _is_previewable(path, mime),
    }


def _is_previewable(path: Path, mime: str) -> bool:
    if path.is_dir():
        return False
    if mime.startswith("text/") or mime in {
        "application/json",
        "application/xml",
        "application/javascript",
    }:
        return True
    return path.suffix.lower() in {
        ".txt", ".md", ".py", ".js", ".css", ".html", ".json", ".csv", ".log", ".yaml", ".yml",
    }


def list_storage(
    relative_path: str | None = "",
    *,
    user_id: str = DEFAULT_USER_ID,
) -> dict[str, Any]:
    storage_dir = _storage_dir(user_id)
    current = _safe_storage_path(relative_path, user_id=user_id)
    if not current.exists():
        raise FileNotFoundError("Path not found.")
    if not current.is_dir():
        raise NotADirectoryError("Path is not a directory.")
    entries = []
    for path in current.iterdir():
        if path.name.startswith(".") or path.is_symlink():
            continue
        resolved = path.resolve()
        if resolved != storage_dir and not resolved.is_relative_to(storage_dir):
            continue
        entries.append(_entry_for(path, user_id=user_id))
    entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
    parent = ""
    if current != storage_dir:
        parent = _storage_rel(current.parent, user_id=user_id)
    return {
        "path": _storage_rel(current, user_id=user_id),
        "parent": parent,
        "entries": entries,
        "record_path": _storage_rel(_analysis_record_path(user_id), user_id=user_id),
    }


def append_analysis_record(
    *,
    user_text: str,
    assistant_text: str,
    user_id: str = DEFAULT_USER_ID,
) -> str:
    analysis_record_path = _analysis_record_path(user_id)
    analysis_record_path.parent.mkdir(parents=True, exist_ok=True)
    entry = (
        f"\n## {_now_iso()}\n\n"
        "USER:\n"
        f"{user_text.strip()}\n\n"
        "AI:\n"
        f"{assistant_text.strip()}\n"
    )
    with analysis_record_path.open("a", encoding="utf-8") as handle:
        handle.write(entry)
    return _storage_rel(analysis_record_path, user_id=user_id)


@dataclass(frozen=True)
class ConfiguredAuthUser:
    user_id: str
    password: str
    role: str = "user"


def auth_users() -> dict[str, ConfiguredAuthUser]:
    raw_users = os.environ.get("WEB_USERS_JSON", "").strip()
    if raw_users:
        payload = json.loads(raw_users)
        if not isinstance(payload, dict):
            raise ValueError("WEB_USERS_JSON must be a JSON object.")
        users = {}
        for raw_user_id, raw_config in payload.items():
            user_id = normalize_user_id(raw_user_id)
            if isinstance(raw_config, str):
                password = raw_config
                role = "user"
            elif isinstance(raw_config, dict):
                password = str(raw_config.get("password", ""))
                role = normalize_user_role(raw_config.get("role", "user"))
            else:
                raise ValueError(f"Invalid WEB_USERS_JSON entry for {user_id}.")
            if not password:
                raise ValueError(f"Missing password for WEB_USERS_JSON user: {user_id}")
            users[user_id] = ConfiguredAuthUser(user_id=user_id, password=password, role=role)
        if not users:
            raise ValueError("WEB_USERS_JSON must contain at least one user.")
        return users

    password = os.environ.get("WEB_PASSWORD", "")
    if not password:
        return {}
    user_id = normalize_user_id(os.environ.get("WEB_USERNAME", "agent"))
    return {
        user_id: ConfiguredAuthUser(
            user_id=user_id,
            password=password,
            role="admin",
        )
    }


def auth_credentials() -> tuple[str, str] | None:
    """Backward-compatible view for callers that still expect one credential."""
    users = auth_users()
    if not users:
        return None
    first = next(iter(users.values()))
    return first.user_id, first.password


def web_auth_store() -> WebAuthStore:
    return WebAuthStore(None)


def sync_environment_auth_users(store: WebAuthStore) -> None:
    store.sync_environment_users({
        user_id: EnvironmentUser(
            user_id=user.user_id,
            password=user.password,
            role=user.role,
        )
        for user_id, user in auth_users().items()
    })


def registration_enabled() -> bool:
    return _env_flag("WEB_ALLOW_REGISTRATION", default=False)


def anonymous_access_enabled() -> bool:
    return _env_flag("WEB_ALLOW_ANONYMOUS", default=False)


def auth_cookie_secure() -> bool:
    return _env_flag("WEB_COOKIE_SECURE", default=False)


def auth_session_ttl_hours() -> int:
    try:
        return max(1, int(os.environ.get("WEB_SESSION_TTL_HOURS", "168")))
    except ValueError:
        return 168


def _env_flag(name: str, *, default: bool) -> bool:
    fallback = "1" if default else "0"
    return os.environ.get(name, fallback).strip().lower() in {"1", "true", "yes", "on"}


def _read_run_index(limit: int = 100) -> list[dict[str, Any]]:
    if not RUNS_DIR.exists():
        return []
    runs = []
    for run_dir in RUNS_DIR.iterdir():
        if not run_dir.is_dir():
            continue
        run_state = _read_json_file(run_dir / "run_state.json")
        metrics = _read_json_file(run_dir / "metrics.json")
        if not run_state:
            continue
        runs.append({
            "run_id": run_dir.name,
            "session_id": str(run_state.get("session_id") or ""),
            "status": str(run_state.get("status") or ""),
            "mode": str(run_state.get("mode") or ""),
            "started_at": str(run_state.get("started_at") or ""),
            "finished_at": str(run_state.get("finished_at") or ""),
            "reasoning_steps": run_state.get("reasoning_steps", 0),
            "model_calls": metrics.get("model_calls", 0),
            "tool_calls": metrics.get("tool_calls", 0),
        })
    runs.sort(key=lambda item: item.get("started_at", ""), reverse=True)
    return runs[:limit]


def _read_run_detail(run_id: str) -> dict[str, Any]:
    if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
        raise ValueError("Invalid run_id.")
    run_dir = (RUNS_DIR / run_id).resolve()
    if not run_dir.is_relative_to(RUNS_DIR.resolve()):
        raise ValueError("Invalid run_id.")
    if not run_dir.is_dir():
        raise FileNotFoundError(run_id)
    events = _read_trace_jsonl(run_dir / "trace.jsonl")
    return {
        "run_id": run_id,
        "run_state": _read_json_file(run_dir / "run_state.json"),
        "report": _read_json_file(run_dir / "report.json"),
        "metrics": _read_json_file(run_dir / "metrics.json"),
        "events": events,
        "subagents": _build_subagent_logs(events),
    }


def _read_trace_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _build_subagent_logs(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events or []:
        if not _is_subagent_trace_event(event):
            continue
        session_id = str(event.get("session_id") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        key = session_id or str(event.get("span_id") or f"subagent:{len(order)}")
        if key not in groups:
            groups[key] = _empty_subagent_group(key)
            order.append(key)
        group = groups[key]
        group["events"].append(_subagent_event_summary(event))
        group["event_count"] += 1
        name = str(event.get("event") or "")
        if not group["started_at"]:
            group["started_at"] = str(event.get("timestamp") or "")
        group["finished_at"] = str(event.get("timestamp") or group["finished_at"] or "")
        if name == "subagent.started":
            group["agent_type"] = str(payload.get("agent_type") or group["agent_type"])
            group["description"] = str(payload.get("description") or group["description"])
            group["prompt_preview"] = str(payload.get("prompt_preview") or "")
            group["span_id"] = str(event.get("span_id") or group["span_id"])
            group["parent_span_id"] = str(event.get("parent_span_id") or group["parent_span_id"])
        elif name == "subagent.completed":
            group["agent_type"] = str(payload.get("agent_type") or group["agent_type"])
            group["success"] = payload.get("success")
            group["truncated"] = bool(payload.get("truncated"))
            group["stop_reason"] = str(payload.get("stop_reason") or "")
            group["summary_preview"] = str(payload.get("summary_preview") or "")
            group["error_preview"] = str(payload.get("error_preview") or "")
            group["tool_count"] = _int_value(payload.get("tool_count"), group["tool_count"])
            group["reasoning_steps"] = _int_value(payload.get("reasoning_steps"), group["reasoning_steps"])
            group["span_id"] = str(event.get("span_id") or group["span_id"])
        elif name == "model.call.completed":
            group["model_calls"] += 1
            model = str(payload.get("model") or "")
            provider = str(payload.get("provider") or "")
            if model or provider:
                group["models"].add(f"{provider}:{model}".strip(":"))
        elif name == "model.call.failed":
            group["model_calls"] += 1
            group["model_failures"] += 1
        elif name in {"tool.call.completed", "tool.call.failed"}:
            group["tool_calls"] += 1
            tool_name = str(payload.get("tool_name") or "")
            if tool_name:
                group["tools"].add(tool_name)
            if name == "tool.call.failed" or payload.get("status") == "error":
                group["tool_failures"] += 1
            if payload.get("status") == "denied":
                group["tool_denials"] += 1
    result = []
    for key in order:
        group = groups[key]
        group["models"] = sorted(group["models"])
        group["tools"] = sorted(group["tools"])
        result.append(group)
    return result


def _is_subagent_trace_event(event: dict[str, Any]) -> bool:
    session_id = str(event.get("session_id") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return (
        session_id.startswith("subtask:")
        or event.get("event") in {"subagent.started", "subagent.completed"}
        or metadata.get("kind") == "subagent"
    )


def _parent_trace_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        event for event in events or []
        if not _is_subagent_trace_event(event)
    ]


def _empty_subagent_group(session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "agent_type": "",
        "description": "",
        "success": None,
        "truncated": False,
        "stop_reason": "",
        "started_at": "",
        "finished_at": "",
        "span_id": "",
        "parent_span_id": "",
        "prompt_preview": "",
        "summary_preview": "",
        "error_preview": "",
        "reasoning_steps": 0,
        "model_calls": 0,
        "model_failures": 0,
        "tool_calls": 0,
        "tool_count": 0,
        "tool_failures": 0,
        "tool_denials": 0,
        "event_count": 0,
        "models": set(),
        "tools": set(),
        "events": [],
    }


def _subagent_event_summary(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    name = str(event.get("event") or "")
    return {
        "timestamp": str(event.get("timestamp") or ""),
        "step": event.get("step"),
        "event": name,
        "span_id": str(event.get("span_id") or ""),
        "parent_span_id": str(event.get("parent_span_id") or ""),
        "label": _subagent_event_label(name, payload),
        "payload": payload,
    }


def _subagent_event_label(name: str, payload: dict[str, Any]) -> str:
    if name in {"tool.call.completed", "tool.call.failed"}:
        tool_name = str(payload.get("tool_name") or "tool")
        status = str(payload.get("status") or ("failed" if name.endswith("failed") else "ok"))
        preview = str(payload.get("output_preview") or payload.get("error_message") or "")
        return f"{tool_name} · {status} · {_squash(preview, 160)}"
    if name in {"model.call.completed", "model.call.failed"}:
        model = str(payload.get("model") or "")
        preview = str(payload.get("content_preview") or payload.get("error_message") or "")
        return f"{model or 'model'} · {_squash(preview, 160)}"
    if name == "subagent.started":
        return f"started · {payload.get('description') or payload.get('agent_type') or ''}"
    if name == "subagent.completed":
        status = "success" if payload.get("success") else "incomplete"
        return f"{status} · {_squash(str(payload.get('summary_preview') or payload.get('error_preview') or ''), 160)}"
    return _squash(json.dumps(payload, ensure_ascii=False, default=str), 180)


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default or 0)


def _squash(value: str, limit: int) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _render_subagent_logs(subagents: list[dict[str, Any]]) -> str:
    if not subagents:
        return "<p>No subagent trace events recorded for this run.</p>"
    blocks = []
    for index, subagent in enumerate(subagents, 1):
        title = (
            str(subagent.get("description") or "").strip()
            or str(subagent.get("agent_type") or "").strip()
            or f"Subagent {index}"
        )
        status = _subagent_status_label(subagent)
        status_class = _subagent_status_class(subagent)
        badges = [
            ("", f"type: {subagent.get('agent_type') or 'unknown'}"),
            ("", f"steps: {subagent.get('reasoning_steps') or 0}"),
            ("", f"models: {subagent.get('model_calls') or 0}"),
            ("", f"tools: {subagent.get('tool_calls') or subagent.get('tool_count') or 0}"),
        ]
        if subagent.get("tool_denials"):
            badges.append(("warn", f"denied: {subagent.get('tool_denials')}"))
        if subagent.get("stop_reason"):
            badges.append(("warn", f"stop: {subagent.get('stop_reason')}"))
        summary_parts = []
        if subagent.get("prompt_preview"):
            summary_parts.append(
                "<div><div class=\"label\">Prompt</div>"
                f"<pre>{html_escape(str(subagent.get('prompt_preview') or ''))}</pre></div>"
            )
        if subagent.get("summary_preview"):
            summary_parts.append(
                "<div><div class=\"label\">Summary</div>"
                f"<pre>{html_escape(str(subagent.get('summary_preview') or ''))}</pre></div>"
            )
        if subagent.get("error_preview"):
            summary_parts.append(
                "<div><div class=\"label\">Error</div>"
                f"<pre>{html_escape(str(subagent.get('error_preview') or ''))}</pre></div>"
            )
        summary_html = (
            f"<div class=\"subagent-summary\">{''.join(summary_parts)}</div>"
            if summary_parts
            else ""
        )
        event_rows = _render_subagent_event_rows(subagent.get("events") or [])
        blocks.append(
            "<details class=\"subagent\">"
            "<summary>"
            "<div class=\"subagent-title\">"
            f"<strong>{html_escape(title)}</strong>"
            f"<span>{html_escape(str(subagent.get('session_id') or ''))}</span>"
            "</div>"
            "<div class=\"subagent-meta\">"
            f"<span class=\"badge {status_class}\">{html_escape(status)}</span>"
            f"<div>{html_escape(str(subagent.get('started_at') or ''))}</div>"
            "</div>"
            "</summary>"
            "<div class=\"subagent-body\">"
            "<div class=\"subagent-badges\">"
            + "".join(
                f"<span class=\"badge {html_escape(kind)}\">{html_escape(text)}</span>"
                for kind, text in badges
            )
            + "</div>"
            f"{summary_html}"
            "<div class=\"subagent-events\">"
            "<table>"
            "<thead><tr><th>Time</th><th>Step</th><th>Event</th><th>Log</th><th>Payload</th></tr></thead>"
            f"<tbody>{event_rows}</tbody>"
            "</table>"
            "</div>"
            "</div>"
            "</details>"
        )
    return f"<div class=\"subagents\">{''.join(blocks)}</div>"


def _render_subagent_event_rows(events: list[dict[str, Any]]) -> str:
    if not events:
        return '<tr><td colspan="5">No events.</td></tr>'
    return "\n".join(
        "<tr>"
        f"<td>{html_escape(str(event.get('timestamp') or ''))}</td>"
        f"<td>{html_escape(str(event.get('step') or ''))}</td>"
        f"<td>{html_escape(str(event.get('event') or ''))}</td>"
        f"<td>{html_escape(str(event.get('label') or ''))}</td>"
        "<td><details><summary>payload</summary><pre>"
        f"{html_escape(json.dumps(event.get('payload') or {}, ensure_ascii=False, indent=2, default=str))}"
        "</pre></details></td>"
        "</tr>"
        for event in events
    )


def _subagent_status_label(subagent: dict[str, Any]) -> str:
    if subagent.get("success") is True:
        return "success"
    if subagent.get("truncated") or subagent.get("stop_reason"):
        return "incomplete"
    if subagent.get("success") is False:
        return "failed"
    return "running/unknown"


def _subagent_status_class(subagent: dict[str, Any]) -> str:
    if subagent.get("success") is True:
        return "ok"
    if subagent.get("truncated") or subagent.get("stop_reason"):
        return "warn"
    if subagent.get("success") is False:
        return "err"
    return ""


def _trace_page(title: str, body: str) -> str:
    escaped_title = html_escape(title)
    return (
        "<!doctype html>"
        "<html><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{escaped_title}</title>"
        "<style>"
        "body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:24px;color:#1f2937;background:#f8fafc;}"
        "h1{font-size:24px;margin:0 0 16px;}h2{font-size:18px;margin:24px 0 8px;}"
        ".cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0;}"
        ".card{background:white;border:1px solid #d1d5db;padding:10px;}.label{color:#64748b;font-size:12px;}.value{font-size:18px;font-weight:650;margin-top:4px;}"
        ".error{border-left:4px solid #dc2626;background:#fff7f7;padding:10px;margin:8px 0;}"
        ".subagents{display:grid;gap:12px;margin:12px 0 20px;}"
        ".subagent{background:white;border:1px solid #d1d5db;border-radius:8px;overflow:hidden;}"
        ".subagent>summary{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;padding:12px;cursor:pointer;}"
        ".subagent-title{display:grid;gap:4px;min-width:0;}.subagent-title strong{font-size:14px;overflow-wrap:anywhere;}"
        ".subagent-title span,.subagent-meta{color:#64748b;font-size:12px;}.subagent-meta{text-align:right;white-space:nowrap;}"
        ".subagent-body{border-top:1px solid #e5e7eb;padding:12px;display:grid;gap:12px;}"
        ".subagent-badges{display:flex;flex-wrap:wrap;gap:6px;}"
        ".badge{display:inline-flex;align-items:center;min-height:24px;padding:0 8px;border-radius:999px;background:#eef2f7;color:#334155;font-size:12px;}"
        ".badge.ok{background:#dcfce7;color:#166534;}.badge.warn{background:#fef3c7;color:#92400e;}.badge.err{background:#fee2e2;color:#991b1b;}"
        ".subagent-summary{background:#f8fafc;border:1px solid #e5e7eb;border-radius:6px;padding:10px;}"
        ".subagent-events{max-height:520px;overflow:auto;border:1px solid #e5e7eb;border-radius:6px;}"
        ".subagent-events table{border:0;}"
        "table{border-collapse:collapse;width:100%;background:white;border:1px solid #d1d5db;}"
        "th,td{border-bottom:1px solid #e5e7eb;padding:8px;text-align:left;vertical-align:top;font-size:13px;}"
        "th{background:#eef2f7;font-weight:600;}a{color:#155e75;text-decoration:none;}a:hover{text-decoration:underline;}"
        "pre{white-space:pre-wrap;word-break:break-word;margin:0;font-size:12px;line-height:1.4;}"
        "</style>"
        "</head><body>"
        f"{body}"
        "</body></html>"
    )


class RequestHandler(BaseHTTPRequestHandler):
    agent_service: AgentService
    auth_user: AuthenticatedUser

    server_version = "taleclaw/0.1"

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            self._send_static("/login.html")
            return
        if parsed.path.startswith("/static/"):
            self._send_static(parsed.path)
            return
        if parsed.path == "/api/auth/config":
            self._send_json({
                "registration_enabled": registration_enabled(),
            })
            return

        if not self._authorize():
            return

        if parsed.path == "/api/auth/me":
            self._send_json({"user": self._public_user(self._current_user())})
            return

        if parsed.path == "/api/health":
            user = self._current_user()
            coding_workspace = _default_coding_workspace()
            self._send_json({
                "ok": True,
                "workspace": str(ROOT) if user.role == "admin" else "private workspace",
                "coding_workspace": str(coding_workspace) if user.role == "admin" else "",
                "user": {"id": user.user_id, "role": user.role},
                "runtime": "lazy",
                "has_env_file": (ROOT / ".env").exists(),
                "has_deepseek_key": bool(os.environ.get("DEEPSEEK_API_KEY")),
            })
            return

        if parsed.path == "/api/runtime-health":
            try:
                self.agent_service.ensure_started()
                self._send_json({
                    "ok": True,
                    "runtime": "ready",
                    "has_deepseek_key": bool(os.environ.get("DEEPSEEK_API_KEY")),
                })
            except Exception as exc:
                traceback.print_exc()
                self._send_json(
                    {
                        "ok": False,
                        "runtime": "error",
                        "error": _friendly_runtime_error(exc),
                        "error_type": type(exc).__name__,
                    },
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
            return

        if parsed.path == "/runs":
            self._handle_runs_page()
            return

        if parsed.path.startswith("/runs/"):
            self._handle_run_detail_page(parsed.path.removeprefix("/runs/"))
            return

        if parsed.path == "/api/runs":
            self._handle_runs_api()
            return

        if parsed.path == "/api/run":
            params = parse_qs(parsed.query)
            self._handle_run_api(params.get("run_id", [""])[0])
            return

        if parsed.path == "/api/sessions":
            self._send_json({"sessions": read_sessions(self._current_user().user_id)})
            return

        if parsed.path == "/api/session":
            params = parse_qs(parsed.query)
            session_id = params.get("session_id", ["default"])[0]
            self._send_json({
                "session": read_session(
                    session_id,
                    user_id=self._current_user().user_id,
                )
            })
            return

        if parsed.path == "/api/memory":
            self._send_json({"files": read_memory_files(self._current_user().user_id)})
            return

        if parsed.path == "/api/files":
            params = parse_qs(parsed.query)
            path = params.get("path", [""])[0]
            try:
                self._send_json({
                    "files": list_storage(path, user_id=self._current_user().user_id)
                })
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == "/api/files/preview":
            self._handle_file_preview(parsed)
            return

        if parsed.path == "/api/files/download":
            self._handle_file_download(parsed)
            return

        self._send_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/auth/login":
            self._handle_auth_login()
            return
        if parsed.path == "/api/auth/register":
            self._handle_auth_register()
            return
        if parsed.path == "/api/auth/logout":
            self._handle_auth_logout()
            return

        if not self._authorize():
            return

        if parsed.path == "/api/chat":
            self._handle_chat()
            return

        if parsed.path == "/api/chat/stream":
            self._handle_chat_stream()
            return

        if parsed.path == "/api/chat/stop":
            self._handle_chat_stop()
            return

        if parsed.path == "/api/analyze":
            self._handle_analyze()
            return

        if parsed.path == "/api/files/upload":
            self._handle_file_upload()
            return

        if parsed.path == "/api/files/mkdir":
            self._handle_mkdir()
            return

        if parsed.path == "/api/files/rename":
            self._handle_rename()
            return

        if parsed.path == "/api/files/delete":
            self._handle_delete()
            return

        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        if not self._authorize():
            return

        parsed = urlparse(self.path)
        if parsed.path == "/api/session":
            self._handle_session_delete()
            return

        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def _handle_runs_page(self) -> None:
        if not self._require_admin():
            return
        runs = _read_run_index()
        rows = "\n".join(
            "<tr>"
            f"<td><a href=\"/runs/{html_escape(run['run_id'])}\">"
            f"{html_escape(run['run_id'])}</a></td>"
            f"<td>{html_escape(run.get('status', ''))}</td>"
            f"<td>{html_escape(run.get('mode', ''))}</td>"
            f"<td>{html_escape(run.get('session_id', ''))}</td>"
            f"<td>{html_escape(run.get('started_at', ''))}</td>"
            f"<td>{html_escape(str(run.get('model_calls', 0)))}</td>"
            f"<td>{html_escape(str(run.get('tool_calls', 0)))}</td>"
            "</tr>"
            for run in runs
        )
        if not rows:
            rows = '<tr><td colspan="7">No runs yet.</td></tr>'
        self._send_html(_trace_page(
            "Runs",
            (
                "<h1>Runs</h1>"
                "<table>"
                "<thead><tr><th>Run</th><th>Status</th><th>Mode</th>"
                "<th>Session</th><th>Started</th><th>Models</th><th>Tools</th>"
                "</tr></thead>"
                f"<tbody>{rows}</tbody>"
                "</table>"
            ),
        ))

    def _handle_run_detail_page(self, raw_run_id: str) -> None:
        if not self._require_admin():
            return
        run_id = unquote(raw_run_id).strip()
        try:
            detail = _read_run_detail(run_id)
        except FileNotFoundError:
            self._send_html(
                _trace_page("Run not found", "<h1>Run not found</h1>"),
                status=HTTPStatus.NOT_FOUND,
            )
            return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        metrics = detail["metrics"]
        state = detail["run_state"]
        subagents = detail.get("subagents") or []
        parent_events = _parent_trace_events(detail["events"])
        error_events = [
            event for event in parent_events
            if str(event.get("event") or "").endswith(".failed")
            or str(event.get("event") or "") in {"run_failed", "model.route.attempts"}
        ]
        error_blocks = "\n".join(
            "<div class=\"error\">"
            f"<strong>{html_escape(event.get('event', ''))}</strong>"
            f"<pre>{html_escape(json.dumps(event.get('payload') or {}, ensure_ascii=False, indent=2, default=str))}</pre>"
            "</div>"
            for event in error_events
        )
        event_rows = "\n".join(
            "<tr>"
            f"<td>{html_escape(event.get('timestamp', ''))}</td>"
            f"<td>{html_escape(str(event.get('step') or ''))}</td>"
            f"<td>{html_escape(event.get('event', ''))}</td>"
            f"<td>{html_escape(str(event.get('span_id') or ''))}</td>"
            "<td><pre>"
            f"{html_escape(json.dumps(event.get('payload') or {}, ensure_ascii=False, indent=2, default=str))}"
            "</pre></td>"
            "</tr>"
            for event in parent_events
        )
        body = (
            '<p><a href="/runs">Back to runs</a></p>'
            f"<h1>{html_escape(run_id)}</h1>"
            "<div class=\"cards\">"
            f"<div class=\"card\"><div class=\"label\">Status</div><div class=\"value\">{html_escape(str(state.get('status') or ''))}</div></div>"
            f"<div class=\"card\"><div class=\"label\">Run duration</div><div class=\"value\">{html_escape(str(metrics.get('run_duration_ms') or ''))} ms</div></div>"
            f"<div class=\"card\"><div class=\"label\">Model</div><div class=\"value\">{html_escape(str(metrics.get('model_calls', 0)))} ok / {html_escape(str(metrics.get('model_failures', 0)))} fail</div></div>"
            f"<div class=\"card\"><div class=\"label\">Tools</div><div class=\"value\">{html_escape(str(metrics.get('tool_calls', 0)))}</div></div>"
            f"<div class=\"card\"><div class=\"label\">Tokens</div><div class=\"value\">{html_escape(str(metrics.get('total_tokens', 0)))}</div></div>"
            f"<div class=\"card\"><div class=\"label\">Sanitized</div><div class=\"value\">{html_escape(str(metrics.get('sanitized_messages', 0)))}</div></div>"
            f"<div class=\"card\"><div class=\"label\">Subagents</div><div class=\"value\">{html_escape(str(len(subagents)))}</div></div>"
            "</div>"
            f"{'<h2>Errors</h2>' + error_blocks if error_blocks else ''}"
            "<h2>Subagents</h2>"
            f"{_render_subagent_logs(subagents)}"
            "<h2>Metrics</h2>"
            f"<pre>{html_escape(json.dumps(metrics, ensure_ascii=False, indent=2, default=str))}</pre>"
            "<h2>Run State</h2>"
            f"<pre>{html_escape(json.dumps(state, ensure_ascii=False, indent=2, default=str))}</pre>"
            "<h2>Trace</h2>"
            "<table>"
            "<thead><tr><th>Time</th><th>Step</th><th>Event</th><th>Span</th><th>Payload</th></tr></thead>"
            f"<tbody>{event_rows}</tbody>"
            "</table>"
        )
        self._send_html(_trace_page(run_id, body))

    def _handle_runs_api(self) -> None:
        if not self._require_admin():
            return
        self._send_json({"runs": _read_run_index()})

    def _handle_run_api(self, run_id: str) -> None:
        if not self._require_admin():
            return
        try:
            self._send_json(_read_run_detail(run_id))
        except FileNotFoundError:
            self._send_json({"error": "Run not found"}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_chat(self) -> None:
        try:
            payload = self._read_json_body()
            message = str(payload.get("message", "")).strip()
            session_id = str(payload.get("session_id", "default")).strip() or "default"
            workspace_root = str(payload.get("workspace_root", "")).strip() or None
            user = self._current_user()
            if not message:
                self._send_json({"error": "message is required"}, status=HTTPStatus.BAD_REQUEST)
                return

            reply = self.agent_service.ask(
                session_id=session_id,
                content=message,
                user_id=user.user_id,
                user_role=user.role,
                workspace_root=workspace_root,
            )
            self._send_json({
                "reply": reply,
                "session_id": session_id,
                "session": read_session(session_id, user_id=user.user_id),
            })
        except Exception as exc:
            traceback.print_exc()
            self._send_json(
                {
                    "error": _friendly_runtime_error(exc),
                    "error_type": type(exc).__name__,
                },
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )

    def _handle_auth_login(self) -> None:
        try:
            payload = self._read_json_body()
            store = web_auth_store()
            sync_environment_auth_users(store)
            user = store.authenticate(
                user_id=str(payload.get("username", "")),
                password=str(payload.get("password", "")),
            )
            if user is None:
                self._send_json(
                    {"error": "用户名或密码错误。"},
                    status=HTTPStatus.UNAUTHORIZED,
                )
                return
            token = store.create_session(user, ttl_hours=auth_session_ttl_hours())
            self._send_json(
                {"ok": True, "user": self._public_user(user)},
                headers={"Set-Cookie": self._auth_cookie(token)},
            )
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            traceback.print_exc()
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_auth_register(self) -> None:
        if not registration_enabled():
            self._send_json(
                {"error": "当前未开放注册。"},
                status=HTTPStatus.FORBIDDEN,
            )
            return
        try:
            payload = self._read_json_body()
            store = web_auth_store()
            sync_environment_auth_users(store)
            user = store.register(
                user_id=str(payload.get("username", "")),
                password=str(payload.get("password", "")),
            )
            token = store.create_session(user, ttl_hours=auth_session_ttl_hours())
            self._send_json(
                {"ok": True, "user": self._public_user(user)},
                status=HTTPStatus.CREATED,
                headers={"Set-Cookie": self._auth_cookie(token)},
            )
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            traceback.print_exc()
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_auth_logout(self) -> None:
        try:
            web_auth_store().revoke_session(self._request_cookie_token())
        finally:
            self._send_json(
                {"ok": True},
                headers={"Set-Cookie": self._expired_auth_cookie()},
            )

    def _handle_session_delete(self) -> None:
        try:
            payload = self._read_json_body()
            user_id = self._current_user().user_id
            storage_id = _web_storage_id(payload.get("session_id", ""), user_id)
            if not self.agent_service.delete_session(storage_id):
                self._send_json({"error": "Session not found"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json({
                "deleted": True,
                "session_id": parse_web_session_id(storage_id)[1],
                "sessions": read_sessions(user_id),
            })
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            traceback.print_exc()
            self._send_json(
                {
                    "error": _friendly_runtime_error(exc),
                    "error_type": type(exc).__name__,
                },
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )

    def _handle_chat_stream(self) -> None:
        try:
            payload = self._read_json_body()
            message = str(payload.get("message", "")).strip()
            session_id = str(payload.get("session_id", "default")).strip() or "default"
            workspace_root = str(payload.get("workspace_root", "")).strip() or None
            user = self._current_user()
            if not message:
                self._send_json({"error": "message is required"}, status=HTTPStatus.BAD_REQUEST)
                return
        except Exception as exc:
            self._send_json(
                {"error": str(exc), "error_type": type(exc).__name__},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        events: queue.Queue[dict[str, Any]] = queue.Queue()

        def run_agent() -> None:
            try:
                reply = self.agent_service.ask_stream(
                    session_id=session_id,
                    content=message,
                    user_id=user.user_id,
                    user_role=user.role,
                    workspace_root=workspace_root,
                    on_text=lambda text: events.put({"type": "delta", "text": text}),
                )
                events.put({
                    "type": "complete",
                    "reply": reply,
                    "session_id": session_id,
                    "session": read_session(session_id, user_id=user.user_id),
                })
            except Exception as exc:
                traceback.print_exc()
                events.put({
                    "type": "error",
                    "error": _friendly_runtime_error(exc),
                    "error_type": type(exc).__name__,
                })

        worker = threading.Thread(
            target=run_agent,
            name=f"agent-web-stream-{session_id}",
            daemon=True,
        )
        worker.start()

        self._send_stream_headers()
        while True:
            event = events.get()
            try:
                self._send_stream_event(event)
            except (BrokenPipeError, ConnectionResetError):
                return
            if event["type"] in {"complete", "error"}:
                return

    def _handle_chat_stop(self) -> None:
        try:
            payload = self._read_json_body()
            user_id = self._current_user().user_id
            storage_id = _web_storage_id(
                str(payload.get("session_id", "default")).strip() or "default",
                user_id,
            )
            requested = self.agent_service.request_cancel(storage_id)
            self._send_json({
                "requested": requested,
                "session_id": parse_web_session_id(storage_id)[1],
            })
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            traceback.print_exc()
            self._send_json(
                {
                    "error": _friendly_runtime_error(exc),
                    "error_type": type(exc).__name__,
                },
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )

    def _handle_analyze(self) -> None:
        try:
            payload = self._read_json_body()
            text = str(payload.get("text", "")).strip()
            session_id = str(payload.get("session_id", "analysis")).strip() or "analysis"
            user = self._current_user()
            if not text:
                self._send_json({"error": "text is required"}, status=HTTPStatus.BAD_REQUEST)
                return
            prompt = (
                "请分析下面这段文字，给出清晰、有结构的回复。"
                "请保留关键事实、判断可能的问题，并给出可执行建议。\n\n"
                f"{text}"
            )
            reply = self.agent_service.ask(
                session_id=session_id,
                content=prompt,
                user_id=user.user_id,
                user_role=user.role,
            )
            record_path = append_analysis_record(
                user_text=text,
                assistant_text=reply,
                user_id=user.user_id,
            )
            self._send_json({
                "reply": reply,
                "record_path": record_path,
                "record_download_url": "/api/files/download?path=" + quote(record_path),
            })
        except Exception as exc:
            traceback.print_exc()
            self._send_json(
                {
                    "error": _friendly_runtime_error(exc),
                    "error_type": type(exc).__name__,
                },
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )

    def _handle_file_preview(self, parsed) -> None:
        try:
            user_id = self._current_user().user_id
            params = parse_qs(parsed.query)
            path = _safe_storage_path(params.get("path", [""])[0], user_id=user_id)
            if not path.exists() or not path.is_file():
                self._send_json({"error": "File not found"}, status=HTTPStatus.NOT_FOUND)
                return
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if not _is_previewable(path, mime):
                self._send_json({"error": "Preview is not available"}, status=HTTPStatus.BAD_REQUEST)
                return
            if path.stat().st_size > MAX_PREVIEW_BYTES:
                self._send_json({"error": "File is too large to preview"}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json({
                "name": path.name,
                "path": _storage_rel(path, user_id=user_id),
                "content": path.read_text(encoding="utf-8", errors="replace"),
                "mime": mime,
            })
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_file_download(self, parsed) -> None:
        try:
            user_id = self._current_user().user_id
            params = parse_qs(parsed.query)
            path = _safe_storage_path(params.get("path", [""])[0], user_id=user_id)
            if not path.exists() or not path.is_file():
                self._send_json({"error": "File not found"}, status=HTTPStatus.NOT_FOUND)
                return
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            safe_name = quote(path.name)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{safe_name}")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_file_upload(self) -> None:
        try:
            user_id = self._current_user().user_id
            storage_dir = _storage_dir(user_id)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length > MAX_BODY_BYTES:
                self._send_json({"error": "Upload is too large"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            fields, files = _parse_multipart_form(self.headers, self.rfile.read(length))
            target_dir = _safe_storage_path((fields.get("path") or [""])[0], user_id=user_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            if not target_dir.is_dir():
                self._send_json({"error": "Target path is not a directory"}, status=HTTPStatus.BAD_REQUEST)
                return
            saved = []
            for item in files:
                if item.field_name != "file" or not item.filename:
                    continue
                dest = (target_dir / item.filename).resolve()
                if dest != storage_dir and not dest.is_relative_to(storage_dir):
                    raise ValueError("Path escapes storage.")
                dest.write_bytes(item.content)
                saved.append(_entry_for(dest, user_id=user_id))
            self._send_json({
                "saved": saved,
                "files": list_storage(
                    _storage_rel(target_dir, user_id=user_id),
                    user_id=user_id,
                ),
            })
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_mkdir(self) -> None:
        try:
            user_id = self._current_user().user_id
            storage_dir = _storage_dir(user_id)
            payload = self._read_json_body()
            parent = _safe_storage_path(payload.get("path", ""), user_id=user_id)
            name = Path(str(payload.get("name", "")).strip()).name
            if not name:
                self._send_json({"error": "name is required"}, status=HTTPStatus.BAD_REQUEST)
                return
            target = (parent / name).resolve()
            if target != storage_dir and not target.is_relative_to(storage_dir):
                raise ValueError("Path escapes storage.")
            target.mkdir(parents=True, exist_ok=True)
            self._send_json({
                "files": list_storage(
                    _storage_rel(parent, user_id=user_id),
                    user_id=user_id,
                )
            })
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_rename(self) -> None:
        try:
            user_id = self._current_user().user_id
            storage_dir = _storage_dir(user_id)
            payload = self._read_json_body()
            source = _safe_storage_path(payload.get("path", ""), user_id=user_id)
            name = Path(str(payload.get("name", "")).strip()).name
            if not name:
                self._send_json({"error": "name is required"}, status=HTTPStatus.BAD_REQUEST)
                return
            if source == storage_dir:
                self._send_json({"error": "Cannot rename storage root"}, status=HTTPStatus.BAD_REQUEST)
                return
            target = (source.parent / name).resolve()
            if target != storage_dir and not target.is_relative_to(storage_dir):
                raise ValueError("Path escapes storage.")
            if target.exists():
                raise FileExistsError("Target already exists.")
            source.rename(target)
            self._send_json({
                "files": list_storage(
                    _storage_rel(target.parent, user_id=user_id),
                    user_id=user_id,
                )
            })
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_delete(self) -> None:
        try:
            user_id = self._current_user().user_id
            storage_dir = _storage_dir(user_id)
            payload = self._read_json_body()
            target = _safe_storage_path(payload.get("path", ""), user_id=user_id)
            if target == storage_dir:
                self._send_json({"error": "Cannot delete storage root"}, status=HTTPStatus.BAD_REQUEST)
                return
            parent = target.parent
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            self._send_json({
                "files": list_storage(
                    _storage_rel(parent, user_id=user_id),
                    user_id=user_id,
                )
            })
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            raise ValueError("Request body is too large.")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _send_json(
        self,
        payload: dict[str, Any],
        *,
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(
        self,
        body: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_stream_headers(self) -> None:
        self.send_response(HTTPStatus.OK)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _send_stream_event(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8") + b"\n"
        self.wfile.write(line)
        self.wfile.flush()

    def _send_static(self, request_path: str) -> None:
        target = self._static_target(request_path)
        if target is None:
            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return

        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static_target(self, request_path: str) -> Path | None:
        path = unquote(request_path.split("?", 1)[0])
        if path in {"", "/"}:
            relative = "index.html"
        else:
            relative = path.lstrip("/")
            if relative.startswith("static/"):
                relative = relative.removeprefix("static/")

        candidate = (STATIC_DIR / relative).resolve()
        if candidate.is_relative_to(STATIC_DIR) and candidate.is_file():
            return candidate

        fallback = STATIC_DIR / "index.html"
        if fallback.exists() and "." not in Path(relative).name:
            return fallback
        return None

    def _authorize(self) -> bool:
        cookie_user = web_auth_store().authenticate_session(self._request_cookie_token())
        if cookie_user is not None:
            self.auth_user = cookie_user
            return True

        users = auth_users()
        header = self.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() == "basic" and token:
            try:
                decoded = base64.b64decode(token).decode("utf-8")
            except Exception:
                decoded = ""
            username, _, password = decoded.partition(":")
            user = users.get(username)
            if (
                user is not None
                and hmac.compare_digest(password, user.password)
            ):
                self.auth_user = AuthenticatedUser(
                    user_id=user.user_id,
                    role=user.role,
                )
                return True

        if anonymous_access_enabled():
            self.auth_user = AuthenticatedUser(
                user_id=DEFAULT_USER_ID,
                role=DEFAULT_USER_ROLE,
            )
            return True

        body = json.dumps({"error": "Authentication required"}, ensure_ascii=False).encode("utf-8")
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._redirect("/login")
        else:
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        return False

    def _current_user(self) -> AuthenticatedUser:
        return getattr(
            self,
            "auth_user",
            AuthenticatedUser(
                user_id=DEFAULT_USER_ID,
                role=DEFAULT_USER_ROLE,
            ),
        )

    def _require_admin(self) -> bool:
        if self._current_user().role == "admin":
            return True
        self._send_json({"error": "Admin role required"}, status=HTTPStatus.FORBIDDEN)
        return False

    def _request_cookie_token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get(AUTH_COOKIE_NAME)
        return morsel.value if morsel is not None else ""

    def _auth_cookie(self, token: str) -> str:
        parts = [
            f"{AUTH_COOKIE_NAME}={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={auth_session_ttl_hours() * 3600}",
        ]
        if auth_cookie_secure():
            parts.append("Secure")
        return "; ".join(parts)

    def _expired_auth_cookie(self) -> str:
        parts = [
            f"{AUTH_COOKIE_NAME}=",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            "Max-Age=0",
        ]
        if auth_cookie_secure():
            parts.append("Secure")
        return "; ".join(parts)

    def _public_user(self, user: AuthenticatedUser) -> dict[str, str]:
        return {"id": user.user_id, "role": user.role}

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))


def build_handler(agent_service: AgentService):
    class BoundRequestHandler(RequestHandler):
        pass

    BoundRequestHandler.agent_service = agent_service
    return BoundRequestHandler


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return int(default)
    try:
        return int(value)
    except ValueError:
        return int(default)


def _default_coding_workspace() -> Path:
    raw = os.environ.get("DEFAULT_CODING_WORKSPACE") or str(ROOT)
    return Path(raw).expanduser().resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local agent web UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()

    agent_service = AgentService()
    server = ThreadingHTTPServer((args.host, args.port), build_handler(agent_service))
    server.daemon_threads = True
    url = f"http://{args.host}:{args.port}"
    print(f"taleclaw running at {url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        agent_service.stop()
        server.server_close()


if __name__ == "__main__":
    main()
