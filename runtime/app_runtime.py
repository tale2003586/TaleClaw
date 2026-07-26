import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from runtime.messaging.events import InboundMessage
from runtime.messaging.user_bus import MessageBus
from runtime.agent_loop import AgentLoop


@dataclass
class AppRuntime:
    bus: MessageBus
    loop: AgentLoop
    services: Any = None
    minecraft_application: Any = None
    _dispatch_task: asyncio.Task | None = field(default=None, init=False)

    def start(self) -> None:
        if self._dispatch_task is None:
            self._dispatch_task = asyncio.create_task(
                self.bus.dispatch_outbound(),
                name="outbound_dispatch",
            )

    async def stop(self) -> None:
        self.bus.stop()
        if self._dispatch_task is not None:
            await self._dispatch_task
        if self.minecraft_application is not None:
            self.minecraft_application.close()

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
        await self.loop.run_once(on_text=on_text)

    async def run_message(
        self,
        *,
        content: str,
        channel: str = "cli",
        chat_id: str = "local",
        sender: str = "user",
        metadata: dict[str, Any] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> None:
        await self.loop.run_inbound(
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
        return self.loop.request_cancel(session_id)

    def request_task_cancel(self, task_id: str) -> bool:
        if self.minecraft_application is None:
            return False
        return bool(self.minecraft_application.cancel_task(task_id))
