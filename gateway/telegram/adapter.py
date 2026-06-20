from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from bus.events import OutboundMessage
from gateway.base import ChannelAdapter
from gateway.telegram.client import TelegramBotApiClient
from gateway.telegram.identity import TelegramIdentity, TelegramIdentityResolver
from gateway.telegram.storage import (
    list_storage_text,
    preview_storage_text,
    resolve_download_path,
    storage_help_text,
)
from gateway.telegram.store import (
    TelegramGatewayStore,
    external_chat_id_from_runtime,
)


logger = logging.getLogger(__name__)


class TelegramGateway(ChannelAdapter):
    """Long-polling Telegram adapter for private text conversations."""

    channel = "telegram"

    def __init__(
        self,
        *,
        runtime,
        client: TelegramBotApiClient,
        identities: TelegramIdentityResolver,
        store: TelegramGatewayStore,
        poll_timeout: int = 30,
        retry_delay: float = 3.0,
    ) -> None:
        self.runtime = runtime
        self.client = client
        self.identities = identities
        self.store = store
        self.poll_timeout = max(1, int(poll_timeout))
        self.retry_delay = max(0.0, float(retry_delay))
        self.outbox_limit = _env_int("TELEGRAM_OUTBOX_BATCH_SIZE", default=10, minimum=1)
        self.outbox_max_attempts = _env_int("TELEGRAM_OUTBOX_MAX_ATTEMPTS", default=3, minimum=1)
        self._stopping = False

    @classmethod
    def from_env(cls, runtime) -> "TelegramGateway":
        return cls(
            runtime=runtime,
            client=TelegramBotApiClient(os.environ.get("TELEGRAM_BOT_TOKEN", "")),
            identities=TelegramIdentityResolver.from_env(),
            store=TelegramGatewayStore(),
            poll_timeout=_env_int("TELEGRAM_POLL_TIMEOUT", default=30, minimum=1),
            retry_delay=_env_float("TELEGRAM_RETRY_DELAY", default=3.0, minimum=0.0),
        )

    async def run_forever(self) -> None:
        self.runtime.bus.subscribe_outbound(self.channel, self.send)
        self.runtime.start()
        while not self._stopping:
            try:
                await self.flush_outbox()
                await self.poll_once()
                await self.flush_outbox()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram polling failed; retrying.")
                await asyncio.sleep(self.retry_delay)

    async def poll_once(self) -> None:
        updates = await self.client.get_updates(
            offset=self.store.get_offset(),
            timeout=self.poll_timeout,
        )
        for update in updates:
            update_id = update.get("update_id")
            if not isinstance(update_id, int):
                logger.warning("Ignoring Telegram update without integer update_id.")
                continue
            try:
                await self.handle_update(update)
            finally:
                self.store.set_offset(update_id + 1)

    async def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return
        if chat.get("type") != "private":
            logger.info("Ignoring non-private Telegram chat %s.", chat.get("id"))
            return

        external_chat_id = _integer_id(chat.get("id"))
        telegram_user_id = _integer_id(sender.get("id"))
        if external_chat_id is None or telegram_user_id is None:
            return

        identity = self.identities.resolve(telegram_user_id)
        if identity is None:
            await self.client.send_message(
                external_chat_id,
                (
                    "当前 Telegram 账号尚未授权。\n"
                    f"你的 Telegram user ID：{telegram_user_id}\n"
                    "请让管理员将该 ID 加入 TELEGRAM_ALLOWED_USER_IDS "
                    "或 TELEGRAM_USER_MAP。"
                ),
            )
            return

        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            await self.client.send_message(
                external_chat_id,
                "当前版本仅支持私聊文字消息。",
            )
            return

        command = _command_name(text)
        if command == "/start":
            await self.client.send_message(
                external_chat_id,
                (
                    "taleclaw 已连接。\n"
                    "直接发送文字即可开始对话。\n"
                    "可用命令：/new、/status、/files、/cat、/download"
                ),
            )
            return
        if command in {"/help", "/files_help"}:
            await self.client.send_message(external_chat_id, storage_help_text())
            return
        if command == "/new":
            self.store.start_conversation(external_chat_id)
            await self.client.send_message(external_chat_id, "已创建新的对话。")
            return
        if command == "/status":
            await self.client.send_message(
                external_chat_id,
                _status_text(identity, self.store.get_conversation_id(external_chat_id)),
            )
            return
        if command in {"/files", "/ls"}:
            try:
                reply = list_storage_text(identity.user_id, _command_arg(text))
            except Exception as exc:
                reply = str(exc)
            await self.client.send_message(external_chat_id, reply)
            return
        if command == "/cat":
            path_arg = _command_arg(text)
            if not path_arg:
                await self.client.send_message(external_chat_id, "用法：/cat <文件路径>")
                return
            try:
                reply = preview_storage_text(identity.user_id, path_arg)
            except Exception as exc:
                reply = str(exc)
            await self.client.send_message(external_chat_id, reply)
            return
        if command == "/download":
            path_arg = _command_arg(text)
            if not path_arg:
                await self.client.send_message(external_chat_id, "用法：/download <文件路径>")
                return
            try:
                path = resolve_download_path(identity.user_id, path_arg)
            except Exception as exc:
                await self.client.send_message(external_chat_id, str(exc))
                return
            await self.client.send_document(
                external_chat_id,
                path,
                caption=f"storage/{path_arg.strip().lstrip('/')}",
            )
            return

        await self.client.send_chat_action(external_chat_id)
        await self.runtime.submit_user_message(
            content=text.strip(),
            channel=self.channel,
            chat_id=self.store.runtime_chat_id(
                external_chat_id,
                user_id=identity.user_id,
            ),
            sender=f"telegram:{telegram_user_id}",
            metadata={
                "user_id": identity.user_id,
                "user_role": identity.role,
                "gateway": self.channel,
                "telegram_user_id": telegram_user_id,
                "telegram_chat_id": external_chat_id,
            },
        )
        await self.runtime.run_once()

    async def send(self, message: OutboundMessage) -> None:
        external_chat_id = external_chat_id_from_runtime(message.chat_id)
        await self.client.send_message(external_chat_id, message.content)

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
                    await self.client.send_message(item["chat_id"], item["text"])
                self.store.mark_message_sent(item["id"])
            except Exception as exc:
                logger.exception("Telegram outbox delivery failed.")
                self.store.mark_message_failed(
                    item["id"],
                    error=f"{type(exc).__name__}: {exc}",
                    max_attempts=self.outbox_max_attempts,
                )

    async def close(self) -> None:
        self._stopping = True
        await self.runtime.stop()
        sessions = getattr(getattr(self.runtime, "loop", None), "sessions", None)
        if sessions is not None:
            sessions.close()
        self.store.close()
        await self.client.close()


def _status_text(identity: TelegramIdentity, conversation_id: str) -> str:
    return (
        "taleclaw Telegram gateway\n"
        f"用户：{identity.user_id}\n"
        f"角色：{identity.role}\n"
        f"会话：{conversation_id}"
    )


def _integer_id(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _command_name(text: str) -> str:
    head = str(text or "").strip().split(maxsplit=1)[0].lower()
    return head.split("@", 1)[0]


def _command_arg(text: str) -> str:
    parts = str(text or "").strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


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
