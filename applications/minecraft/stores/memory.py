from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from ..models import (
    MinecraftCheckpoint,
    MinecraftTask,
    MinecraftTaskEvent,
    TaskStatus,
)


class StoreConflict(RuntimeError):
    pass


class InMemoryMinecraftTaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, MinecraftTask] = {}
        self._idempotency: dict[str, str] = {}
        self._events: dict[str, list[MinecraftTaskEvent]] = {}
        self._event_ids: set[str] = set()
        self._checkpoints: dict[str, MinecraftCheckpoint] = {}
        self._leases: dict[str, tuple[str, datetime]] = {}
        self._lock = threading.RLock()

    def create(
        self,
        task: MinecraftTask,
        *,
        idempotency_key: str,
    ) -> MinecraftTask:
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        with self._lock:
            existing_id = self._idempotency.get(key)
            if existing_id:
                return self._copy(self._tasks[existing_id])
            active = self._active_for_bot(task.bot_id)
            if active is not None:
                raise StoreConflict(
                    f"bot {task.bot_id} already has active task {active.task_id}"
                )
            self._tasks[task.task_id] = self._copy(task)
            self._idempotency[key] = task.task_id
            self._events[task.task_id] = []
            return self._copy(task)

    def get(self, task_id: str) -> MinecraftTask | None:
        with self._lock:
            task = self._tasks.get(str(task_id))
            return self._copy(task) if task else None

    def update(
        self,
        task: MinecraftTask,
        *,
        expected_version: int,
    ) -> MinecraftTask:
        with self._lock:
            current = self._tasks.get(task.task_id)
            if current is None:
                raise KeyError(task.task_id)
            if current.version != int(expected_version):
                raise StoreConflict(
                    f"version conflict: expected {expected_version}, got {current.version}"
                )
            if task.version <= current.version:
                task = task.model_copy(update={"version": current.version + 1})
            self._tasks[task.task_id] = self._copy(task)
            return self._copy(task)

    def append_event(self, event: MinecraftTaskEvent) -> None:
        with self._lock:
            if event.task_id not in self._tasks:
                raise KeyError(event.task_id)
            if event.event_id in self._event_ids:
                return
            self._event_ids.add(event.event_id)
            self._events.setdefault(event.task_id, []).append(event.model_copy(deep=True))

    def events(self, task_id: str) -> tuple[MinecraftTaskEvent, ...]:
        with self._lock:
            return tuple(item.model_copy(deep=True) for item in self._events.get(task_id, ()))

    def save_checkpoint(self, checkpoint: MinecraftCheckpoint) -> None:
        with self._lock:
            if checkpoint.task_id not in self._tasks:
                raise KeyError(checkpoint.task_id)
            current = self._checkpoints.get(checkpoint.task_id)
            if current is not None and checkpoint.version < current.version:
                raise StoreConflict("checkpoint version cannot move backwards")
            self._checkpoints[checkpoint.task_id] = checkpoint.model_copy(deep=True)

    def latest_checkpoint(self, task_id: str) -> MinecraftCheckpoint | None:
        with self._lock:
            item = self._checkpoints.get(task_id)
            return item.model_copy(deep=True) if item else None

    def list_recoverable(self) -> list[MinecraftTask]:
        with self._lock:
            return [
                self._copy(task)
                for task in self._tasks.values()
                if not task.status.terminal
            ]

    def active_for_bot(self, bot_id: str) -> MinecraftTask | None:
        with self._lock:
            active = self._active_for_bot(bot_id)
            return self._copy(active) if active else None

    def request_cancel(self, task_id: str) -> MinecraftTask:
        with self._lock:
            current = self._tasks.get(task_id)
            if current is None:
                raise KeyError(task_id)
            if current.status.terminal or current.cancel_requested:
                return self._copy(current)
            updated = current.model_copy(
                update={"cancel_requested": True, "version": current.version + 1}
            )
            self._tasks[task_id] = updated
            return self._copy(updated)

    def acquire_lease(
        self, task_id: str, *, owner_id: str, ttl_seconds: float
    ) -> bool:
        with self._lock:
            if task_id not in self._tasks:
                raise KeyError(task_id)
            now = datetime.now(timezone.utc)
            lease = self._leases.get(task_id)
            if lease and lease[0] != owner_id and lease[1] > now:
                return False
            self._leases[task_id] = (
                owner_id,
                now + timedelta(seconds=max(0.1, float(ttl_seconds))),
            )
            return True

    def renew_lease(
        self, task_id: str, *, owner_id: str, ttl_seconds: float
    ) -> bool:
        with self._lock:
            lease = self._leases.get(task_id)
            if lease is None or lease[0] != owner_id:
                return False
            self._leases[task_id] = (
                owner_id,
                datetime.now(timezone.utc)
                + timedelta(seconds=max(0.1, float(ttl_seconds))),
            )
            return True

    def release_lease(self, task_id: str, *, owner_id: str) -> bool:
        with self._lock:
            lease = self._leases.get(task_id)
            if lease is None or lease[0] != owner_id:
                return False
            del self._leases[task_id]
            return True

    def _active_for_bot(self, bot_id: str) -> MinecraftTask | None:
        return next(
            (
                task
                for task in self._tasks.values()
                if task.bot_id == bot_id and not task.status.terminal
            ),
            None,
        )

    @staticmethod
    def _copy(task: MinecraftTask) -> MinecraftTask:
        return task.model_copy(deep=True)
