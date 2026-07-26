from __future__ import annotations

import asyncio
import threading
from concurrent.futures import TimeoutError as FutureTimeout

from .models import ResourceGoal
from .service import MinecraftTaskService


class MinecraftApplication:
    """Synchronous application facade backed by a dedicated asyncio loop."""

    def __init__(
        self,
        *,
        service: MinecraftTaskService,
        operation_timeout: float = 15,
    ) -> None:
        self.service = service
        self.operation_timeout = max(1, float(operation_timeout))
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="minecraft-application",
            daemon=True,
        )
        self._thread.start()

    def start_task(
        self,
        *,
        text: str | None = None,
        goal: ResourceGoal | None = None,
        user_id: str,
        session_id: str,
        bot_id: str,
        idempotency_key: str | None = None,
    ):
        return self._submit(
            self.service.create_task(
                text=text,
                goal=goal,
                user_id=user_id,
                session_id=session_id,
                bot_id=bot_id,
                idempotency_key=idempotency_key,
                start_background=True,
            )
        )

    def get_status(self, task_id: str, *, user_id=None, session_id=None):
        return self.service.get_status(
            task_id,
            user_id=user_id,
            session_id=session_id,
        )

    def cancel_task(self, task_id: str, *, user_id=None, session_id=None) -> bool:
        return bool(
            self._submit(
                self.service.cancel_task(
                    task_id,
                    user_id=user_id,
                    session_id=session_id,
                )
            )
        )

    def resume_recoverable(self):
        return self._submit(self.service.resume_recoverable())

    def close(self) -> None:
        if self._loop.is_closed():
            return
        try:
            self._submit(self.service.close())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=self.operation_timeout)
            self._loop.close()

    def _submit(self, coroutine):
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout=self.operation_timeout)
        except FutureTimeout as exc:
            future.cancel()
            raise TimeoutError("Minecraft application operation timed out") from exc

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
