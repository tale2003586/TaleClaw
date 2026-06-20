from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from bus.events import OutboundMessage
from gateway.base import ChannelAdapter
from gateway.feishu.client import FeishuApiClient
from gateway.feishu.identity import FeishuIdentity, FeishuIdentityResolver
from gateway.feishu.store import FeishuGatewayStore, external_chat_id_from_runtime
from gateway.telegram.storage import (
    list_storage_text,
    preview_storage_text,
    resolve_download_path,
    storage_help_text,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CallbackResponse:
    payload: dict[str, Any]
    status: HTTPStatus = HTTPStatus.OK


class FeishuGateway(ChannelAdapter):
    """HTTP callback adapter for Feishu bot conversations."""

    channel = "feishu"

    def __init__(
        self,
        *,
        runtime,
        client: FeishuApiClient,
        identities: FeishuIdentityResolver,
        store: FeishuGatewayStore,
        host: str = "127.0.0.1",
        port: int = 8010,
        callback_path: str = "/feishu/events",
        verification_token: str = "",
        respond_in_groups: bool = False,
        outbox_poll_seconds: float = 2.0,
    ) -> None:
        self.runtime = runtime
        self.client = client
        self.identities = identities
        self.store = store
        self.host = str(host or "127.0.0.1")
        self.port = max(1, int(port))
        self.callback_path = _normalized_path(callback_path)
        self.verification_token = str(verification_token or "").strip()
        self.respond_in_groups = bool(respond_in_groups)
        self.outbox_poll_seconds = max(0.2, float(outbox_poll_seconds))
        self.outbox_limit = _env_int("FEISHU_OUTBOX_BATCH_SIZE", default=10, minimum=1)
        self.outbox_max_attempts = _env_int("FEISHU_OUTBOX_MAX_ATTEMPTS", default=3, minimum=1)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: ThreadingHTTPServer | None = None
        self._stopping = False
        self._runtime_lock = asyncio.Lock()

    @classmethod
    def from_env(cls, runtime) -> "FeishuGateway":
        return cls(
            runtime=runtime,
            client=FeishuApiClient(
                os.environ.get("FEISHU_APP_ID", ""),
                os.environ.get("FEISHU_APP_SECRET", ""),
            ),
            identities=FeishuIdentityResolver.from_env(),
            store=FeishuGatewayStore(),
            host=os.environ.get("FEISHU_CALLBACK_HOST", "127.0.0.1"),
            port=_env_int("FEISHU_CALLBACK_PORT", default=8010, minimum=1),
            callback_path=os.environ.get("FEISHU_CALLBACK_PATH", "/feishu/events"),
            verification_token=os.environ.get("FEISHU_VERIFICATION_TOKEN", ""),
            respond_in_groups=_env_bool("FEISHU_RESPOND_IN_GROUPS", default=False),
            outbox_poll_seconds=_env_float("FEISHU_OUTBOX_POLL_SECONDS", default=2.0, minimum=0.2),
        )

    async def run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.runtime.bus.subscribe_outbound(self.channel, self.send)
        self.runtime.start()
        handler = self._build_handler()
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        server_thread = asyncio.create_task(
            asyncio.to_thread(self._server.serve_forever, 0.5),
            name="feishu-callback-http-server",
        )
        try:
            while not self._stopping:
                await self.flush_outbox()
                await asyncio.sleep(self.outbox_poll_seconds)
        finally:
            if self._server is not None:
                await asyncio.to_thread(self._server.shutdown)
            await server_thread

    async def handle_callback(self, payload: dict[str, Any]) -> CallbackResponse:
        if self._is_encrypted_payload(payload):
            return CallbackResponse(
                {"error": "Encrypted Feishu callbacks are not supported yet."},
                HTTPStatus.BAD_REQUEST,
            )
        if not self._verify_token(payload):
            return CallbackResponse({"error": "Invalid Feishu verification token."}, HTTPStatus.FORBIDDEN)
        if _is_url_verification(payload):
            return CallbackResponse({"challenge": str(payload.get("challenge", ""))})

        event_id = _event_id(payload)
        if event_id and not self.store.mark_event_seen(event_id):
            return CallbackResponse({"ok": True, "duplicate": True})

        if _event_type(payload) == "im.message.receive_v1":
            asyncio.create_task(self._handle_message_event(payload), name=f"feishu-event-{event_id or 'message'}")
        return CallbackResponse({"ok": True})

    async def send(self, message: OutboundMessage) -> None:
        external_chat_id = external_chat_id_from_runtime(message.chat_id)
        await self.client.send_message(external_chat_id, message.content, receive_id_type="chat_id")

    async def flush_outbox(self) -> None:
        for item in self.store.list_pending_messages(limit=self.outbox_limit):
            try:
                if item.get("message_type") == "document":
                    await self.client.send_document(
                        item["chat_id"],
                        item["document_path"],
                        caption=item.get("caption", ""),
                    )
                else:
                    await self.client.send_message(
                        item["chat_id"],
                        item["text"],
                        receive_id_type="chat_id",
                    )
                self.store.mark_message_sent(item["id"])
            except Exception as exc:
                logger.exception("Feishu outbox delivery failed.")
                self.store.mark_message_failed(
                    item["id"],
                    error=f"{type(exc).__name__}: {exc}",
                    max_attempts=self.outbox_max_attempts,
                )

    async def close(self) -> None:
        self._stopping = True
        if self._server is not None:
            await asyncio.to_thread(self._server.shutdown)
        await self.runtime.stop()
        sessions = getattr(getattr(self.runtime, "loop", None), "sessions", None)
        if sessions is not None:
            sessions.close()
        self.store.close()
        await self.client.close()

    async def _handle_message_event(self, payload: dict[str, Any]) -> None:
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
        feishu_open_id = str(sender_id.get("open_id") or "").strip()
        chat_id = str(message.get("chat_id") or "").strip()
        chat_type = str(message.get("chat_type") or "").strip()
        if not chat_id or not feishu_open_id:
            return
        if chat_type != "p2p" and not self.respond_in_groups and not _mentions_bot(payload):
            return

        identity = self.identities.resolve(feishu_open_id)
        if identity is None:
            await self.client.send_message(
                chat_id,
                (
                    "当前飞书账号尚未授权。\n"
                    f"你的 Feishu open_id：{feishu_open_id}\n"
                    "请让管理员将该 ID 加入 FEISHU_ALLOWED_OPEN_IDS 或 FEISHU_USER_MAP。"
                ),
                receive_id_type="chat_id",
            )
            return

        text = _message_text(message)
        if not text:
            await self.client.send_message(chat_id, "当前版本仅支持飞书文字消息。", receive_id_type="chat_id")
            return
        await self._dispatch_text(chat_id=chat_id, open_id=feishu_open_id, text=text, identity=identity)

    async def _dispatch_text(
        self,
        *,
        chat_id: str,
        open_id: str,
        text: str,
        identity: FeishuIdentity,
    ) -> None:
        command = _command_name(text)
        if command == "/start":
            await self.client.send_message(
                chat_id,
                (
                    "taleclaw 已连接飞书。\n"
                    "直接发送文字即可开始对话。\n"
                    "可用命令：/new、/status、/files、/cat、/download"
                ),
                receive_id_type="chat_id",
            )
            return
        if command in {"/help", "/files_help"}:
            await self.client.send_message(chat_id, storage_help_text(), receive_id_type="chat_id")
            return
        if command == "/new":
            self.store.start_conversation(chat_id)
            await self.client.send_message(chat_id, "已创建新的对话。", receive_id_type="chat_id")
            return
        if command == "/status":
            await self.client.send_message(
                chat_id,
                _status_text(identity, self.store.get_conversation_id(chat_id), chat_id),
                receive_id_type="chat_id",
            )
            return
        if command in {"/files", "/ls"}:
            try:
                reply = list_storage_text(identity.user_id, _command_arg(text))
            except Exception as exc:
                reply = str(exc)
            await self.client.send_message(chat_id, reply, receive_id_type="chat_id")
            return
        if command == "/cat":
            path_arg = _command_arg(text)
            if not path_arg:
                await self.client.send_message(chat_id, "用法：/cat <文件路径>", receive_id_type="chat_id")
                return
            try:
                reply = preview_storage_text(
                    identity.user_id,
                    path_arg,
                    max_bytes=_env_int("FEISHU_STORAGE_PREVIEW_BYTES", default=8000, minimum=1),
                )
            except Exception as exc:
                reply = str(exc)
            await self.client.send_message(chat_id, reply, receive_id_type="chat_id")
            return
        if command == "/download":
            path_arg = _command_arg(text)
            if not path_arg:
                await self.client.send_message(chat_id, "用法：/download <文件路径>", receive_id_type="chat_id")
                return
            try:
                path = resolve_download_path(
                    identity.user_id,
                    path_arg,
                    max_bytes=_env_int(
                        "FEISHU_STORAGE_DOWNLOAD_MAX_BYTES",
                        default=10 * 1024 * 1024,
                        minimum=1,
                    ),
                )
            except Exception as exc:
                await self.client.send_message(chat_id, str(exc), receive_id_type="chat_id")
                return
            await self.client.send_document(chat_id, path, caption=f"storage/{path_arg.strip().lstrip('/')}")
            return

        async with self._runtime_lock:
            await self.runtime.submit_user_message(
                content=text.strip(),
                channel=self.channel,
                chat_id=self.store.runtime_chat_id(chat_id, user_id=identity.user_id),
                sender=f"feishu:{open_id}",
                metadata={
                    "user_id": identity.user_id,
                    "user_role": identity.role,
                    "gateway": self.channel,
                    "feishu_open_id": open_id,
                    "feishu_chat_id": chat_id,
                },
            )
            await self.runtime.run_once()

    def _build_handler(self):
        gateway = self

        class FeishuCallbackHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path != gateway.callback_path:
                    self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    max_bytes = _env_int("FEISHU_CALLBACK_MAX_BYTES", default=1024 * 1024, minimum=1024)
                    if length <= 0 or length > max_bytes:
                        self._send_json({"error": "Invalid request body size."}, HTTPStatus.BAD_REQUEST)
                        return
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("callback payload must be a JSON object")
                    response = gateway._submit_callback(payload)
                    self._send_json(response.payload, response.status)
                except Exception as exc:
                    logger.exception("Failed to handle Feishu callback.")
                    self._send_json(
                        {"error": f"{type(exc).__name__}: {exc}"},
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send_json({"ok": True, "gateway": "feishu"})
                    return
                self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

            def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt: str, *args: Any) -> None:
                logger.info("Feishu callback: " + fmt, *args)

        return FeishuCallbackHandler

    def _submit_callback(self, payload: dict[str, Any]) -> CallbackResponse:
        if self._loop is None:
            raise RuntimeError("FeishuGateway is not running.")
        future = asyncio.run_coroutine_threadsafe(self.handle_callback(payload), self._loop)
        return future.result(timeout=_env_int("FEISHU_CALLBACK_ACK_TIMEOUT", default=5, minimum=1))

    def _verify_token(self, payload: dict[str, Any]) -> bool:
        if not self.verification_token:
            return True
        token = _callback_token(payload)
        return token == self.verification_token

    def _is_encrypted_payload(self, payload: dict[str, Any]) -> bool:
        return bool(payload.get("encrypt"))


def _status_text(identity: FeishuIdentity, conversation_id: str, chat_id: str) -> str:
    return (
        "taleclaw Feishu gateway\n"
        f"用户：{identity.user_id}\n"
        f"角色：{identity.role}\n"
        f"飞书 chat_id：{chat_id}\n"
        f"会话：{conversation_id}"
    )


def _message_text(message: dict[str, Any]) -> str:
    if message.get("message_type") != "text":
        return ""
    raw_content = message.get("content")
    if isinstance(raw_content, str):
        try:
            content = json.loads(raw_content)
        except json.JSONDecodeError:
            return raw_content.strip()
    elif isinstance(raw_content, dict):
        content = raw_content
    else:
        return ""
    text = str(content.get("text") or "").strip()
    return _strip_at_mentions(text)


def _strip_at_mentions(text: str) -> str:
    import re

    return re.sub(r"<at\s+[^>]*>.*?</at>", "", text).strip()


def _mentions_bot(payload: dict[str, Any]) -> bool:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    mentions = message.get("mentions")
    bot_open_id = os.environ.get("FEISHU_BOT_OPEN_ID", "").strip()
    if not isinstance(mentions, list):
        return False
    if not bot_open_id:
        return bool(mentions)
    for item in mentions:
        if not isinstance(item, dict):
            continue
        key = item.get("key") or item.get("id") or {}
        if isinstance(key, dict) and key.get("open_id") == bot_open_id:
            return True
        if str(item.get("open_id") or "") == bot_open_id:
            return True
    return False


def _event_type(payload: dict[str, Any]) -> str:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    return str(header.get("event_type") or payload.get("type") or "")


def _event_id(payload: dict[str, Any]) -> str:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    return str(header.get("event_id") or payload.get("uuid") or "")


def _callback_token(payload: dict[str, Any]) -> str:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    return str(header.get("token") or payload.get("token") or "")


def _is_url_verification(payload: dict[str, Any]) -> bool:
    return payload.get("type") == "url_verification" and "challenge" in payload


def _command_name(text: str) -> str:
    head = str(text or "").strip().split(maxsplit=1)[0].lower()
    return head.split("@", 1)[0]


def _command_arg(text: str) -> str:
    parts = str(text or "").strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _normalized_path(value: str) -> str:
    path = str(value or "").strip() or "/feishu/events"
    return path if path.startswith("/") else f"/{path}"


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, *, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _env_float(name: str, *, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default))))
    except ValueError:
        return default
