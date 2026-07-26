from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from memory.domain import MemoryIndexOperation, MemoryStatus


@dataclass(frozen=True)
class IndexSyncResult:
    claimed: int = 0
    completed: int = 0
    retried: int = 0


class MemoryIndexSynchronizer:
    def __init__(
        self,
        repository,
        index,
        *,
        retry_base_seconds: int = 5,
        trace: Callable[..., None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.index = index
        self.retry_base_seconds = max(1, int(retry_base_seconds))
        self.trace = trace
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def drain(self, limit: int = 100) -> IndexSyncResult:
        events = self.repository.claim_index_events(limit)
        completed = 0
        retried = 0
        for event in events:
            try:
                item = self.repository.get(event.memory_id)
                if event.operation is MemoryIndexOperation.UPSERT:
                    if (
                        item is not None
                        and item.version == event.memory_version
                        and item.status is MemoryStatus.ACTIVE
                        and item.is_retrievable(self.clock())
                    ):
                        self.index.upsert(item)
                    else:
                        self.index.delete(event.memory_id)
                else:
                    self.index.delete(event.memory_id)
                self.repository.complete_index_event(event.id)
                completed += 1
                self._emit("memory.index.completed", event, "")
            except Exception as exc:
                delay = self.retry_base_seconds * (2 ** min(event.attempt_count, 8))
                self.repository.retry_index_event(
                    event.id,
                    error=f"{type(exc).__name__}: {exc}",
                    next_attempt_at=self.clock() + timedelta(seconds=delay),
                )
                retried += 1
                self._emit("memory.index.failed", event, f"{type(exc).__name__}: {exc}")
        return IndexSyncResult(len(events), completed, retried)

    def _emit(self, name: str, event, error: str) -> None:
        if self.trace is None:
            return
        payload = {
            "outbox_event_id": event.id,
            "memory_id": event.memory_id,
            "memory_version": event.memory_version,
            "operation": event.operation.value,
            "attempt_count": event.attempt_count,
            "index_status": "failed" if error else "completed",
            "error": error[:500],
        }
        try:
            self.trace(name, payload)
        except TypeError:
            self.trace({"event": name, "payload": payload})
