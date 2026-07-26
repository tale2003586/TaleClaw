from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Protocol

from runtime.cancellation import CancellationToken

from .models import (
    ActionEvent,
    ActionHandle,
    BotObservation,
    BridgeAction,
    MinecraftCheckpoint,
    MinecraftTask,
    MinecraftTaskEvent,
)


class MinecraftTaskStore(Protocol):
    def create(
        self,
        task: MinecraftTask,
        *,
        idempotency_key: str,
    ) -> MinecraftTask: ...

    def get(self, task_id: str) -> MinecraftTask | None: ...

    def update(
        self,
        task: MinecraftTask,
        *,
        expected_version: int,
    ) -> MinecraftTask: ...

    def append_event(self, event: MinecraftTaskEvent) -> None: ...

    def save_checkpoint(self, checkpoint: MinecraftCheckpoint) -> None: ...

    def latest_checkpoint(self, task_id: str) -> MinecraftCheckpoint | None: ...

    def list_recoverable(self) -> list[MinecraftTask]: ...

    def active_for_bot(self, bot_id: str) -> MinecraftTask | None: ...

    def request_cancel(self, task_id: str) -> MinecraftTask: ...

    def acquire_lease(
        self, task_id: str, *, owner_id: str, ttl_seconds: float
    ) -> bool: ...

    def renew_lease(
        self, task_id: str, *, owner_id: str, ttl_seconds: float
    ) -> bool: ...

    def release_lease(self, task_id: str, *, owner_id: str) -> bool: ...


class BridgeClient(Protocol):
    async def connect(self) -> BotObservation: ...

    async def observe(self) -> BotObservation: ...

    async def submit_action(self, action: BridgeAction) -> ActionHandle: ...

    async def watch_action(
        self,
        action_id: str,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ActionEvent]: ...

    async def cancel_action(self, action_id: str) -> None: ...

    async def disconnect(self) -> None: ...


class MinecraftTraceSink(Protocol):
    def emit(self, event: MinecraftTaskEvent) -> None: ...


class MinecraftProgressPublisher(Protocol):
    def publish(self, task: MinecraftTask, event: MinecraftTaskEvent) -> None: ...


class NullTraceSink:
    def emit(self, event: MinecraftTaskEvent) -> None:
        return None


class NullProgressPublisher:
    def publish(self, task: MinecraftTask, event: MinecraftTaskEvent) -> None:
        return None


WorkerFactory = Callable[[str], None]
