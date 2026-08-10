import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from runtime.messaging.events import InboundMessage
from runtime.messaging.user_bus import MessageBus
from applications.turn_coordinator import TurnCoordinator


logger = logging.getLogger(__name__)


@dataclass
class AppRuntime:
    bus: MessageBus
    coordinator: TurnCoordinator
    services: Any = None
    _dispatch_task: asyncio.Task | None = field(default=None, init=False)
    _stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _closed: bool = field(default=False, init=False)

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("AppRuntime has already been closed.")
        if self._dispatch_task is None or self._dispatch_task.done():
            self._dispatch_task = asyncio.create_task(
                self.bus.dispatch_outbound(),
                name="outbound_dispatch",
            )

    async def stop(self) -> None:
        async with self._stop_lock:
            if self._closed:
                return
            self.bus.stop()
            task = self._dispatch_task
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                results = await asyncio.gather(task, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException) and not isinstance(
                        result, asyncio.CancelledError
                    ):
                        logger.error(
                            "outbound dispatcher failed during shutdown",
                            exc_info=(type(result), result, result.__traceback__),
                        )
            self._dispatch_task = None
            self._closed = True

    async def submit_user_message(
        self,
        content: str,
        channel: str = "cli",
        chat_id: str = "local",
        sender: str = "user",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.bus.publish_inbound(InboundMessage(
            channel=channel,
            chat_id=chat_id,
            sender=sender,
            content=content,
            metadata=metadata or {},
        ))

    async def run_once(self, on_text: Callable[[str], None] | None = None) -> None:
        await self.coordinator.run_once(on_text=on_text)

    async def run_message(
        self,
        *,
        content: str,
        channel: str = "cli",
        chat_id: str = "local",
        sender: str = "user",
        metadata: dict[str, Any] | None = None,
        on_text: Callable[[str], None] | None = None,
    ):
        return await self.coordinator.run_inbound(
            InboundMessage(
                channel=channel,
                chat_id=chat_id,
                sender=sender,
                content=content,
                metadata=metadata or {},
            ),
            on_text=on_text,
        )

    def request_cancel(self, session_id: str) -> bool:
        return self.coordinator.request_cancel(session_id)
