from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx


TELEGRAM_TEXT_LIMIT = 4096
SAFE_TEXT_CHUNK_SIZE = 4000


class TelegramBotApiError(RuntimeError):
    pass


class TelegramBotApiClient:
    """Small async client for the Telegram Bot API methods used by taleclaw."""

    def __init__(
        self,
        token: str,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        cleaned_token = str(token or "").strip()
        if not cleaned_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required.")
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{cleaned_token}/",
            timeout=httpx.Timeout(45.0, connect=10.0),
            proxy=os.environ.get("TELEGRAM_PROXY_URL") or None,
            trust_env=False,
        )

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = 30,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": max(1, int(timeout)),
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = int(offset)
        result = await self._call("getUpdates", payload, timeout=max(timeout + 10, 15))
        if not isinstance(result, list):
            raise TelegramBotApiError("Telegram getUpdates returned an invalid result.")
        return [item for item in result if isinstance(item, dict)]

    async def send_message(self, chat_id: int | str, text: str) -> None:
        for chunk in split_telegram_text(text):
            await self._call("sendMessage", {
                "chat_id": chat_id,
                "text": chunk,
            })

    async def send_chat_action(self, chat_id: int | str, action: str = "typing") -> None:
        await self._call("sendChatAction", {
            "chat_id": chat_id,
            "action": action,
        })

    async def send_document(
        self,
        chat_id: int | str,
        path: str | Path,
        *,
        caption: str = "",
    ) -> None:
        document_path = Path(path)
        mime = "application/octet-stream"
        try:
            import mimetypes

            mime = mimetypes.guess_type(document_path.name)[0] or mime
        except Exception:
            pass
        try:
            with document_path.open("rb") as handle:
                response = await self._client.post(
                    "sendDocument",
                    data={
                        "chat_id": str(chat_id),
                        "caption": str(caption or "")[:1024],
                    },
                    files={
                        "document": (document_path.name, handle, mime),
                    },
                )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramBotApiError(
                _request_failure_message("sendDocument", exc)
            ) from None
        if not isinstance(data, dict) or not data.get("ok"):
            description = data.get("description") if isinstance(data, dict) else None
            raise TelegramBotApiError(
                f"Telegram Bot API rejected sendDocument: {description or 'unknown error'}"
            )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        timeout: int | float | None = None,
    ) -> Any:
        try:
            response = await self._client.post(
                method,
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramBotApiError(
                _request_failure_message(method, exc)
            ) from None
        if not isinstance(data, dict) or not data.get("ok"):
            description = data.get("description") if isinstance(data, dict) else None
            raise TelegramBotApiError(
                f"Telegram Bot API rejected {method}: {description or 'unknown error'}"
            )
        return data.get("result")


def _request_failure_message(method: str, exc: Exception) -> str:
    prefix = f"Telegram Bot API request failed for {method}:"
    if isinstance(exc, httpx.ProxyError):
        return f"{prefix} proxy connection failed. Check TELEGRAM_PROXY_URL."
    if isinstance(exc, httpx.ConnectTimeout):
        return (
            f"{prefix} connection timed out. Check server access to api.telegram.org "
            "or configure TELEGRAM_PROXY_URL."
        )
    if isinstance(exc, httpx.ReadTimeout):
        return f"{prefix} response timed out. Check the Telegram API connection."
    if isinstance(exc, httpx.ConnectError):
        return (
            f"{prefix} could not connect to api.telegram.org. "
            "Check network access or TELEGRAM_PROXY_URL."
        )
    if isinstance(exc, httpx.TimeoutException):
        return f"{prefix} request timed out. Check the Telegram API connection."
    if isinstance(exc, httpx.HTTPStatusError):
        return _http_status_failure_message(method, exc.response.status_code)
    if isinstance(exc, httpx.HTTPError):
        return f"{prefix} network request failed."
    return f"{prefix} Telegram returned an invalid JSON response."


def _http_status_failure_message(method: str, status_code: int) -> str:
    prefix = f"Telegram Bot API request failed for {method}: HTTP {status_code}."
    if status_code in {401, 404}:
        return f"{prefix} Check TELEGRAM_BOT_TOKEN."
    if status_code == 409:
        return (
            f"{prefix} Remove an existing webhook or stop any other getUpdates worker."
        )
    if status_code == 429:
        return f"{prefix} Telegram rate limit reached; retry later."
    return prefix


def split_telegram_text(text: str, *, limit: int = SAFE_TEXT_CHUNK_SIZE) -> list[str]:
    """Split plain text conservatively below Telegram's sendMessage limit."""

    content = str(text or "").strip()
    if not content:
        return ["(empty response)"]
    chunk_limit = max(1, min(int(limit), TELEGRAM_TEXT_LIMIT))
    chunks: list[str] = []
    while len(content) > chunk_limit:
        split_at = content.rfind("\n", 0, chunk_limit + 1)
        if split_at < chunk_limit // 2:
            split_at = content.rfind(" ", 0, chunk_limit + 1)
        if split_at < chunk_limit // 2:
            split_at = chunk_limit
        chunks.append(content[:split_at].rstrip())
        content = content[split_at:].lstrip()
    if content:
        chunks.append(content)
    return chunks
