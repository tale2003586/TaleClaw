from __future__ import annotations

import argparse
import asyncio
import json
import os

from applications.minecraft.bridge_client import HttpBridgeClient


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="TaleClaw Minecraft Bridge smoke test")
    result.add_argument(
        "--check-only",
        action="store_true",
        help="Validate configuration without opening a network connection (default).",
    )
    result.add_argument(
        "--connect",
        action="store_true",
        help="Explicitly connect to the configured dedicated test server.",
    )
    return result


async def run(connect: bool) -> dict:
    base_url = os.getenv("MINECRAFT_BRIDGE_URL", "http://127.0.0.1:8765")
    token = os.getenv("MINECRAFT_BRIDGE_TOKEN", "")
    result = {
        "bridge_url": base_url,
        "token_configured": bool(token),
        "mode": "connect" if connect else "check-only",
    }
    if not token:
        result["ok"] = False
        result["error"] = "MINECRAFT_BRIDGE_TOKEN is required"
        return result
    client = HttpBridgeClient(base_url=base_url, token=token)
    if not connect:
        await client._client.aclose()
        result["ok"] = True
        return result
    try:
        observation = await client.connect()
        result.update(
            {
                "ok": True,
                "bot_id": observation.bot_id,
                "version": observation.version,
                "server_id": observation.server_id,
            }
        )
        return result
    finally:
        await client.disconnect()


def main() -> int:
    args = parser().parse_args()
    result = asyncio.run(run(connect=bool(args.connect)))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
