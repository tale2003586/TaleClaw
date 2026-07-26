from __future__ import annotations

from collections.abc import Callable

from .models import MinecraftTask, MinecraftTaskEvent


class MinecraftProgressPublisher:
    """Publishes already-throttled user-facing summaries through an adapter."""

    def __init__(self, publish: Callable[[dict], None], *, every: int = 5) -> None:
        self.publish_callback = publish
        self.every = max(1, int(every))
        self._counts: dict[str, int] = {}

    def publish(self, task: MinecraftTask, event: MinecraftTaskEvent) -> None:
        count = self._counts.get(task.task_id, 0) + 1
        self._counts[task.task_id] = count
        if count % self.every and not task.status.terminal:
            return
        self.publish_callback(
            {
                "task_id": task.task_id,
                "session_id": task.session_id,
                "status": task.status.value,
                "net_acquired": task.net_acquired,
                "event": event.event_type,
            }
        )
