import asyncio

import httpx
import pytest

from applications.minecraft.bridge_client import BridgeError, HttpBridgeClient
from applications.minecraft.models import BridgeAction, BridgeActionType
from runtime.cancellation import CancellationRegistry


def observation():
    return {
        "connected": True,
        "bot_id": "bot",
        "server_id": "server",
        "world_id": "world",
        "version": "1.21.1",
        "position": {"x": 0, "y": 64, "z": 0},
        "inventory": [],
    }


def test_remote_url_requires_explicit_trust():
    with pytest.raises(ValueError):
        HttpBridgeClient(base_url="https://bridge.example", token="secret")


def test_connect_action_watch_and_cancel():
    asyncio.run(_connect_action_watch_and_cancel())


async def _connect_action_watch_and_cancel():
    calls = []
    polls = iter(["pending", "running", "succeeded"])

    async def handler(request):
        calls.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/v1/bot/connect":
            return httpx.Response(200, json=observation())
        if request.url.path == "/v1/actions" and request.method == "POST":
            return httpx.Response(202, json={"action_id": "a1", "status": "pending"})
        if request.url.path == "/v1/actions/a1" and request.method == "GET":
            status = next(polls)
            return httpx.Response(200, json={"action_id": "a1", "status": status})
        if request.url.path == "/v1/bot/disconnect":
            return httpx.Response(200, json={"disconnected": True})
        return httpx.Response(404, json={"error": "not_found"})

    client = HttpBridgeClient(
        base_url="http://127.0.0.1:8765",
        token="secret",
        poll_interval=0.001,
        transport=httpx.MockTransport(handler),
    )
    assert (await client.connect()).version == "1.21.1"
    handle = await client.submit_action(
        BridgeAction(
            type=BridgeActionType.FIND_BLOCKS,
            arguments={"resource": "oak_log", "count": 1},
            idempotency_key="find-00001",
        )
    )
    token = CancellationRegistry().register("watch")
    events = [event async for event in client.watch_action(handle.action_id, token)]
    assert [event.status.value for event in events] == ["pending", "running", "succeeded"]
    assert all(auth == "Bearer secret" for _, _, auth in calls)
    await client.disconnect()


def test_errors_do_not_expose_token():
    asyncio.run(_errors_do_not_expose_token())


async def _errors_do_not_expose_token():
    async def handler(_request):
        return httpx.Response(401, json={"error": "unauthorized"})

    client = HttpBridgeClient(
        base_url="http://127.0.0.1:8765",
        token="top-secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(BridgeError) as exc:
        await client.observe()
    assert exc.value.code == "unauthorized"
    assert "top-secret" not in str(exc.value)
    await client._client.aclose()
