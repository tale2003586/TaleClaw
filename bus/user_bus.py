# bus/user_bus.py

import asyncio
import logging
from collections.abc import Awaitable, Callable

from bus.events import InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)


class MessageBus:
    def __init__(self) -> None:
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._subscribers: dict[
            str,
            list[Callable[[OutboundMessage], Awaitable[None]]],
        ] = {}
        self._running = False

    async def publish_inbound(self, message: InboundMessage) -> None:
        await self._inbound.put(message)

    async def consume_inbound(self) -> InboundMessage:
        return await self._inbound.get()

    async def publish_outbound(self, message: OutboundMessage) -> None:
        await self._outbound.put(message)

    def subscribe_outbound(
        self,
        channel: str,
        handler: Callable[[OutboundMessage], Awaitable[None]],
    ) -> None:
        self._subscribers.setdefault(channel, []).append(handler)

    async def dispatch_outbound(self) -> None:
        self._running = True
        while self._running:
            try:
                message = await asyncio.wait_for(self._outbound.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            for handler in self._subscribers.get(message.channel, []):
                try:
                    await handler(message)
                except Exception as first_error:
                    logger.warning("outbound dispatch failed, retrying: %s", first_error)
                    await asyncio.sleep(1)
                    try:
                        await handler(message)
                    except Exception as second_error:
                        logger.error("outbound dispatch failed after retry: %s", second_error)

    def stop(self) -> None:
        self._running = False
