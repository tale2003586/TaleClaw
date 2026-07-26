from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import httpx

from runtime.cancellation import CancellationToken

from .models import (
    ActionEvent,
    ActionHandle,
    ActionStatus,
    BotObservation,
    BridgeAction,
)


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class HttpBridgeClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        trust_remote: bool = False,
        poll_interval: float = 0.25,
        timeout_seconds: float = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        _validate_base_url(base_url, trust_remote=trust_remote)
        if not str(token or "").strip():
            raise ValueError("Bridge token is required")
        self.base_url = base_url.rstrip("/")
        self._token = str(token)
        self.poll_interval = max(0.01, float(poll_interval))
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    async def health(self) -> dict:
        return await self._request("GET", "/health", authenticated=False)

    async def connect(self) -> BotObservation:
        payload = await self._request("POST", "/v1/bot/connect")
        return BotObservation.model_validate(payload)

    async def observe(self) -> BotObservation:
        payload = await self._request("GET", "/v1/bot/state")
        return BotObservation.model_validate(payload)

    async def submit_action(self, action: BridgeAction) -> ActionHandle:
        payload = await self._request(
            "POST",
            "/v1/actions",
            json=action.model_dump(mode="json"),
        )
        return ActionHandle.model_validate(payload)

    async def watch_action(
        self,
        action_id: str,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ActionEvent]:
        last_status: ActionStatus | None = None
        while True:
            if cancellation_token.requested():
                await self.cancel_action(action_id)
                yield ActionEvent(action_id=action_id, status=ActionStatus.CANCELLED)
                return
            payload = await self._request("GET", f"/v1/actions/{action_id}")
            status = ActionStatus(payload["status"])
            if status != last_status or status.terminal:
                yield ActionEvent(
                    action_id=action_id,
                    status=status,
                    progress=payload.get("progress"),
                    error_code=payload.get("error_code"),
                    message=str(payload.get("message") or ""),
                )
                last_status = status
            if status.terminal:
                return
            await asyncio.sleep(self.poll_interval)

    async def cancel_action(self, action_id: str) -> None:
        await self._request("POST", f"/v1/actions/{action_id}/cancel")

    async def disconnect(self) -> None:
        try:
            await self._request("POST", "/v1/bot/disconnect")
        finally:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        **kwargs,
    ) -> dict:
        headers = kwargs.pop("headers", {})
        if not authenticated:
            headers = {**headers, "Authorization": ""}
        try:
            response = await self._client.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise BridgeError("bridge_timeout") from exc
        except httpx.HTTPError as exc:
            raise BridgeError("bridge_network_error") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise BridgeError("invalid_bridge_response") from exc
        if response.is_error:
            code = str(payload.get("error") or f"http_{response.status_code}")
            message = str(payload.get("message") or code)
            raise BridgeError(code, message)
        if not isinstance(payload, dict):
            raise BridgeError("invalid_bridge_response")
        return payload


def _validate_base_url(base_url: str, *, trust_remote: bool) -> None:
    parsed = urlparse(str(base_url))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Bridge URL must be an absolute HTTP(S) URL")
    if trust_remote:
        return
    hostname = parsed.hostname.lower()
    if hostname == "localhost":
        return
    try:
        if ipaddress.ip_address(hostname).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError("remote Bridge URL requires explicit trust_remote=True")
