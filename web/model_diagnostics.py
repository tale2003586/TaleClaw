"""Explicit, bounded model-list probes for administrator drafts.

This never sends a generation request. DNS is checked once and the connection
uses that exact IP, preserving the configured hostname for Host and TLS SNI.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from web.runtime_settings import RuntimeSettingsStore, SettingsValidationError


PROBE_TIMEOUT_SECONDS = 8.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_MODELS = 200
MAX_MODEL_ID_LENGTH = 200
_PROBE_SLOTS = threading.BoundedSemaphore(2)
_DNS_SLOTS = threading.BoundedSemaphore(2)
_METADATA_HOSTS = {"metadata", "metadata.google.internal", "instance-data.ec2.internal"}
_METADATA_IPS = {ipaddress.ip_address("100.100.100.200"), ipaddress.ip_address("fd00:ec2::254")}
_MESSAGES = {
    "ok": "鉴权与模型列表接口可用；这不代表模型生成、配额或所选协议已验证。",
    "unsupported": "该服务未提供模型列表接口；仍可手动填写模型并保存。",
    "authentication_failed": "服务拒绝鉴权，请检查 API Key 与接口地址。",
    "unavailable": "暂时无法获取模型列表，请检查网络、服务状态或稍后重试。",
    "invalid_response": "模型列表响应格式不兼容或超过限制；仍可手动填写模型并保存。",
}


def _safe_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
        effective = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address
        if (effective.is_link_local or effective.is_unspecified or effective.is_multicast
                or effective in _METADATA_IPS or address in _METADATA_IPS):
            raise ValueError()
        return str(address)
    except ValueError:
        raise SettingsValidationError("此接口目标不可用于连接测试。") from None


def _resolve_target(hostname: str, port: int, deadline: float) -> str:
    """Bound DNS wait and work slots, then reject every forbidden answer."""
    normalized_host = hostname.lower().rstrip(".")
    if normalized_host in _METADATA_HOSTS or "%" in normalized_host:
        raise SettingsValidationError("此接口目标不可用于连接测试。")
    try:
        literal = ipaddress.ip_address(normalized_host)
    except ValueError:
        pass
    else:
        return _safe_ip(str(literal))
    if not _DNS_SLOTS.acquire(blocking=False):
        raise TimeoutError()
    finished = threading.Event()
    answers: list = []

    def resolve() -> None:
        try:
            answers.extend(socket.getaddrinfo(normalized_host, port, type=socket.SOCK_STREAM))
        except OSError:
            pass
        finally:
            _DNS_SLOTS.release()
            finished.set()

    # A stuck OS resolver cannot extend the caller's deadline or create an
    # unbounded queue. Its slot remains occupied until that resolver returns.
    threading.Thread(target=resolve, daemon=True, name="model-probe-dns").start()
    if not finished.wait(max(0.0, deadline - time.monotonic())) or not answers:
        raise TimeoutError()
    addresses = [_safe_ip(answer[4][0]) for answer in answers]
    return addresses[0]


async def _request_models(profile: dict, address: str, *, transport=None) -> dict:
    url = urlsplit(profile["base_url"])
    port = url.port or (443 if url.scheme == "https" else 80)
    ip_host = f"[{address}]" if ":" in address else address
    target = urlunsplit((url.scheme, f"{ip_host}:{port}", url.path.rstrip("/") + "/models", "", ""))
    headers = {"Authorization": f"Bearer {profile['api_key']}", "Host": url.netloc,
               "Accept": "application/json", "Accept-Encoding": "identity"}
    if profile.get("user_agent"):
        headers["User-Agent"] = profile["user_agent"]
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS, follow_redirects=False,
                                 trust_env=False, transport=transport) as client:
        async with client.stream("GET", target, headers=headers,
                                 extensions={"sni_hostname": url.hostname}) as response:
            if response.status_code in (401, 403):
                return {"status": "authentication_failed"}
            if response.status_code in (404, 405, 501):
                return {"status": "unsupported"}
            if not 200 <= response.status_code < 300:
                return {"status": "unavailable"}
            # Never decode compressed data before bounding its allocation.
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                return {"status": "invalid_response"}
            try:
                content_length = int(response.headers.get("content-length", "0"))
            except ValueError:
                return {"status": "invalid_response"}
            if content_length > MAX_RESPONSE_BYTES:
                return {"status": "invalid_response"}
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    return {"status": "invalid_response"}
                body.extend(chunk)
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        return {"status": "invalid_response"}
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return {"status": "invalid_response"}
    models = []
    seen = set()
    truncated = False
    for item in data["data"]:
        model = item.get("id") if isinstance(item, dict) else None
        if (not isinstance(model, str) or not model.strip() or len(model) > MAX_MODEL_ID_LENGTH
                or any(ord(char) < 32 for char in model) or profile["api_key"] in model):
            continue
        model = model.strip()
        if model in seen:
            continue
        seen.add(model)
        if len(models) >= MAX_MODELS:
            truncated = True
            break
        models.append(model)
    return {"status": "ok", "models": models, "truncated": truncated}


def probe_models(store: RuntimeSettingsStore, payload: dict, *, transport=None) -> dict:
    profile = store.prepare_probe(payload)  # Revision/identity errors precede any DNS or network.
    started = time.monotonic()
    if not _PROBE_SLOTS.acquire(blocking=False):
        return {"status": "unavailable", "message": "连接测试正在进行，请稍后重试。",
                "elapsed_ms": 0, "models": [], "truncated": False}
    try:
        deadline = started + PROBE_TIMEOUT_SECONDS
        url = urlsplit(profile["base_url"])
        address = _resolve_target(url.hostname, url.port or (443 if url.scheme == "https" else 80), deadline)

        async def request() -> dict:
            return await asyncio.wait_for(_request_models(profile, address, transport=transport),
                                          timeout=max(0.0, deadline - time.monotonic()))

        try:
            result = asyncio.run(request())
        except (httpx.HTTPError, TimeoutError, OSError, ValueError):
            result = {"status": "unavailable"}
    except TimeoutError:
        result = {"status": "unavailable"}
    finally:
        _PROBE_SLOTS.release()
    return {"status": result["status"], "message": _MESSAGES[result["status"]],
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "models": result.get("models", []), "truncated": result.get("truncated", False)}
