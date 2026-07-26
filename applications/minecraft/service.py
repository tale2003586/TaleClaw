from __future__ import annotations

import asyncio
from uuid import uuid4

from runtime.cancellation import CancellationRegistry

from .models import MinecraftTask, MinecraftTaskEvent, ResourceGoal, TaskStatus, now_iso
from .parser import parse_resource_goal
from .ports import BridgeClient, MinecraftTaskStore
from .worker import MinecraftWorker, WorkerResult


class MinecraftTaskService:
    def __init__(
        self,
        *,
        store: MinecraftTaskStore,
        bridge: BridgeClient,
        cancellations: CancellationRegistry,
        worker: MinecraftWorker | None = None,
    ) -> None:
        self.store = store
        self.bridge = bridge
        self.cancellations = cancellations
        self.worker = worker or MinecraftWorker(
            store=store,
            bridge=bridge,
            cancellations=cancellations,
        )
        self._background: dict[str, asyncio.Task] = {}

    async def create_task(
        self,
        *,
        text: str | None = None,
        goal: ResourceGoal | None = None,
        user_id: str,
        session_id: str,
        bot_id: str,
        idempotency_key: str | None = None,
        start_background: bool = False,
    ) -> MinecraftTask:
        parsed = goal or parse_resource_goal(text or "")
        observation = await self.bridge.connect()
        baseline = observation.item_count(parsed.resource)
        task = MinecraftTask(
            user_id=user_id,
            session_id=session_id,
            bot_id=bot_id,
            goal=parsed,
            status=TaskStatus.OBSERVING,
            baseline_count=baseline,
            current_count=baseline,
            started_at=now_iso(),
            cancellation_scope=f"minecraft:{uuid4().hex}",
        )
        created = self.store.create(
            task,
            idempotency_key=idempotency_key or f"{session_id}:{parsed.resource}:{parsed.quantity}",
        )
        self.store.append_event(
            MinecraftTaskEvent(
                task_id=created.task_id,
                event_type="TASK_CREATED",
                payload={"baseline_count": baseline},
            )
        )
        self.cancellations.register(created.cancellation_scope)
        if start_background:
            self._background[created.task_id] = asyncio.create_task(
                self.worker.run(created.task_id),
                name=f"minecraft:{created.task_id}",
            )
        return created

    async def run_to_completion(self, task_id: str) -> WorkerResult:
        return await self.worker.run(task_id)

    def get_status(
        self,
        task_id: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> MinecraftTask:
        task = self.store.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if user_id is not None and task.user_id != user_id:
            raise PermissionError("task belongs to another user")
        if session_id is not None and task.session_id != session_id:
            raise PermissionError("task belongs to another session")
        return task

    async def cancel_task(
        self,
        task_id: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        task = self.get_status(task_id, user_id=user_id, session_id=session_id)
        if task.status.terminal:
            return task.status is TaskStatus.CANCELLED
        task = self.store.request_cancel(task_id)
        requested = self.cancellations.request(task.cancellation_scope)
        if task.active_action_id:
            await self.bridge.cancel_action(task.active_action_id)
        return requested

    async def resume_recoverable(self) -> tuple[MinecraftTask, ...]:
        """Reconnect, validate checkpoint identity, then resume safe tasks."""
        recovered: list[MinecraftTask] = []
        tasks = self.store.list_recoverable()
        if not tasks:
            return ()
        observation = await self.bridge.connect()
        for task in tasks:
            if task.cancel_requested:
                self.cancellations.register(task.cancellation_scope)
                self.cancellations.request(task.cancellation_scope)
                self._background[task.task_id] = asyncio.create_task(
                    self.worker.run(task.task_id),
                    name=f"minecraft:cancel:{task.task_id}",
                )
                recovered.append(task)
                continue
            checkpoint = self.store.latest_checkpoint(task.task_id)
            if checkpoint is None:
                continue
            identity = checkpoint.payload
            matches = (
                identity.get("bot_id") == observation.bot_id
                and identity.get("server_id") == observation.server_id
                and identity.get("world_id") == observation.world_id
                and identity.get("baseline_count") == task.baseline_count
                and checkpoint.plan_version == task.plan_version
            )
            if not matches:
                previous = task.version
                blocked = task.model_copy(
                    update={
                        "status": TaskStatus.BLOCKED,
                        "error_code": "resume_identity_mismatch",
                        "version": task.version + 1,
                        "updated_at": now_iso(),
                        "finished_at": now_iso(),
                    }
                )
                recovered.append(
                    self.store.update(blocked, expected_version=previous)
                )
                continue
            self.cancellations.register(task.cancellation_scope)
            self._background[task.task_id] = asyncio.create_task(
                self.worker.run(task.task_id),
                name=f"minecraft:resume:{task.task_id}",
            )
            recovered.append(task)
        return tuple(recovered)

    async def close(self) -> None:
        for task in self._background.values():
            if not task.done():
                task.cancel()
        if self._background:
            await asyncio.gather(*self._background.values(), return_exceptions=True)
        self._background.clear()
        await self.bridge.disconnect()
