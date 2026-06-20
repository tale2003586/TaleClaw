from __future__ import annotations

import json
import mimetypes
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx


FEISHU_TEXT_LIMIT = 5000
SAFE_TEXT_CHUNK_SIZE = 4500


class FeishuApiError(RuntimeError):
    pass


class FeishuApiClient:
    """Small async client for the Feishu Open Platform APIs used by taleclaw."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        base_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.app_id = str(app_id or "").strip()
        self.app_secret = str(app_secret or "").strip()
        if not self.app_id:
            raise ValueError("FEISHU_APP_ID is required.")
        if not self.app_secret:
            raise ValueError("FEISHU_APP_SECRET is required.")
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=(base_url or os.environ.get("FEISHU_BASE_URL") or "https://open.feishu.cn"),
            timeout=httpx.Timeout(45.0, connect=10.0),
            proxy=os.environ.get("FEISHU_PROXY_URL") or None,
            trust_env=False,
        )
        self._tenant_access_token = ""
        self._tenant_access_token_expires_at = 0.0

    async def send_message(
        self,
        receive_id: str,
        text: str,
        *,
        receive_id_type: str = "chat_id",
    ) -> None:
        for chunk in split_feishu_text(text):
            await self._call(
                "POST",
                f"/open-apis/im/v1/messages?{urlencode({'receive_id_type': receive_id_type})}",
                json={
                    "receive_id": str(receive_id),
                    "msg_type": "text",
                    "content": json.dumps({"text": chunk}, ensure_ascii=False),
                },
            )

    async def send_document(
        self,
        chat_id: str,
        path: str | Path,
        *,
        caption: str = "",
    ) -> None:
        if caption:
            await self.send_message(chat_id, caption, receive_id_type="chat_id")
        file_key = await self.upload_file(path)
        await self._call(
            "POST",
            f"/open-apis/im/v1/messages?{urlencode({'receive_id_type': 'chat_id'})}",
            json={
                "receive_id": str(chat_id),
                "msg_type": "file",
                "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
            },
        )

    async def upload_file(self, path: str | Path) -> str:
        document_path = Path(path)
        mime = mimetypes.guess_type(document_path.name)[0] or "application/octet-stream"
        try:
            with document_path.open("rb") as handle:
                data = await self._call(
                    "POST",
                    "/open-apis/im/v1/files",
                    data={
                        "file_type": "stream",
                        "file_name": document_path.name,
                    },
                    files={
                        "file": (document_path.name, handle, mime),
                    },
                )
        except OSError as exc:
            raise FeishuApiError(f"Cannot read document for Feishu upload: {exc}") from None
        file_key = ((data.get("data") or {}) if isinstance(data, dict) else {}).get("file_key")
        if not file_key:
            raise FeishuApiError("Feishu upload_file returned no file_key.")
        return str(file_key)

    async def tenant_access_token(self) -> str:
        now = time.time()
        if self._tenant_access_token and now < self._tenant_access_token_expires_at:
            return self._tenant_access_token
        data = await self._call(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self.app_id,
                "app_secret": self.app_secret,
            },
            auth=False,
        )
        token = data.get("tenant_access_token") if isinstance(data, dict) else None
        if not token:
            raise FeishuApiError("Feishu tenant token response did not include tenant_access_token.")
        expires_in = _positive_int(data.get("expire") if isinstance(data, dict) else None, 7200)
        self._tenant_access_token = str(token)
        self._tenant_access_token_expires_at = time.time() + max(60, expires_in - 60)
        return self._tenant_access_token

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        headers = {}
        if auth:
            headers["Authorization"] = f"Bearer {await self.tenant_access_token()}"
        try:
            response = await self._client.request(
                method,
                path,
                json=json,
                data=data,
                files=files,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FeishuApiError(_request_failure_message(method, path, exc)) from None
        if not isinstance(payload, dict):
            raise FeishuApiError(f"Feishu API returned invalid JSON for {path}.")
        code = payload.get("code", 0)
        if code not in {0, "0"}:
            msg = payload.get("msg") or payload.get("message") or "unknown error"
            raise FeishuApiError(f"Feishu API rejected {path}: code={code}, msg={msg}")
        return payload


def split_feishu_text(text: str, *, limit: int = SAFE_TEXT_CHUNK_SIZE) -> list[str]:
    content = str(text or "").strip()
    if not content:
        return ["(empty response)"]
    chunk_limit = max(1, min(int(limit), FEISHU_TEXT_LIMIT))
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


def _request_failure_message(method: str, path: str, exc: Exception) -> str:
    prefix = f"Feishu API request failed for {method} {path}:"
    if isinstance(exc, httpx.ProxyError):
        return f"{prefix} proxy connection failed. Check FEISHU_PROXY_URL."
    if isinstance(exc, httpx.ConnectTimeout):
        return f"{prefix} connection timed out. Check server access to open.feishu.cn."
    if isinstance(exc, httpx.ReadTimeout):
        return f"{prefix} response timed out."
    if isinstance(exc, httpx.ConnectError):
        return f"{prefix} could not connect to open.feishu.cn."
    if isinstance(exc, httpx.TimeoutException):
        return f"{prefix} request timed out."
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{prefix} HTTP {exc.response.status_code}."
    if isinstance(exc, httpx.HTTPError):
        return f"{prefix} network request failed."
    return f"{prefix} Feishu returned an invalid JSON response."


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback
