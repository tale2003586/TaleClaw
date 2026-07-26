from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from threading import RLock
from typing import Sequence
from uuid import uuid4

from memory.commands import MemoryTransition
from memory.domain import (
    MemoryEvidence,
    MemoryIndexOperation,
    MemoryItem,
    MemoryKind,
    MemoryStatus,
    OwnerKey,
    validate_transition,
)
from memory.repository import (
    InvalidTransition,
    MemoryIndexOutboxEvent,
    MemoryNotFound,
    VersionConflict,
)


class InMemoryMemoryRepository:
    def __init__(self) -> None:
        self.items: dict[str, MemoryItem] = {}
        self.evidence: dict[str, list[MemoryEvidence]] = {}
        self.outbox: dict[str, MemoryIndexOutboxEvent] = {}
        self._lock = RLock()

    def create(
        self,
        item: MemoryItem,
        evidence: Sequence[MemoryEvidence] = (),
    ) -> MemoryItem:
        with self._lock:
            if item.id in self.items:
                raise VersionConflict(f"Memory already exists: {item.id}")
            self.items[item.id] = item
            self.evidence[item.id] = [replace(value, memory_id=item.id) for value in evidence]
            if item.status is MemoryStatus.ACTIVE:
                self._schedule(item, MemoryIndexOperation.UPSERT)
            return item

    def get(self, memory_id: str) -> MemoryItem | None:
        return self.items.get(memory_id)

    def get_many(self, memory_ids: Sequence[str]) -> list[MemoryItem]:
        return [self.items[value] for value in memory_ids if value in self.items]

    def list_active(self, scopes: Sequence[OwnerKey], now: datetime) -> list[MemoryItem]:
        allowed = set(scopes)
        return [
            item for item in self.items.values()
            if item.owner in allowed and item.is_retrievable(now)
        ]

    def find_exact(
        self,
        owner: OwnerKey,
        kind: MemoryKind,
        normalized: str,
    ) -> MemoryItem | None:
        matches = [
            item for item in self.items.values()
            if item.owner == owner
            and item.kind is MemoryKind(kind)
            and item.normalized_content == normalized
            and item.status in {MemoryStatus.CANDIDATE, MemoryStatus.ACTIVE}
        ]
        return max(matches, key=lambda item: item.updated_at, default=None)

    def transition(self, command: MemoryTransition) -> MemoryItem:
        with self._lock:
            item = self.items.get(command.memory_id)
            if item is None:
                raise MemoryNotFound(command.memory_id)
            if item.version != command.expected_version:
                raise VersionConflict(command.memory_id)
            try:
                updated = item.transitioned(
                    command.target_status,
                    metadata={**command.metadata, "reason": command.reason},
                )
            except ValueError as exc:
                raise InvalidTransition(str(exc)) from exc
            self.items[item.id] = updated
            operation = (
                MemoryIndexOperation.UPSERT
                if updated.status is MemoryStatus.ACTIVE
                else MemoryIndexOperation.DELETE
            )
            self._schedule(updated, operation)
            return updated

    def supersede(
        self,
        old_memory_id: str,
        new_item: MemoryItem,
        evidence: Sequence[MemoryEvidence],
        *,
        expected_version: int,
    ) -> MemoryItem:
        with self._lock:
            old = self.items.get(old_memory_id)
            if old is None:
                raise MemoryNotFound(old_memory_id)
            if old.version != expected_version:
                raise VersionConflict(old_memory_id)
            if new_item.supersedes_id != old.id or new_item.owner != old.owner:
                raise InvalidTransition("Invalid superseding memory chain.")
            try:
                superseded = old.transitioned(MemoryStatus.SUPERSEDED)
            except ValueError as exc:
                raise InvalidTransition(str(exc)) from exc
            self.items[old.id] = superseded
            self.items[new_item.id] = new_item
            self.evidence[new_item.id] = [replace(value, memory_id=new_item.id) for value in evidence]
            self._schedule(superseded, MemoryIndexOperation.DELETE)
            self._schedule(new_item, MemoryIndexOperation.UPSERT)
            return new_item

    def add_evidence(
        self,
        memory_id: str,
        evidence: Sequence[MemoryEvidence],
        *,
        expected_version: int | None = None,
    ) -> MemoryItem:
        with self._lock:
            item = self.items.get(memory_id)
            if item is None:
                raise MemoryNotFound(memory_id)
            if expected_version is not None and item.version != expected_version:
                raise VersionConflict(memory_id)
            known = {value.id for value in self.evidence.setdefault(memory_id, [])}
            self.evidence[memory_id].extend(
                replace(value, memory_id=memory_id)
                for value in evidence
                if value.id not in known
            )
            updated = replace(item, updated_at=datetime.now(timezone.utc))
            self.items[memory_id] = updated
            return updated

    def list_evidence(self, memory_id: str) -> list[MemoryEvidence]:
        return list(self.evidence.get(memory_id, []))

    def claim_index_events(self, limit: int = 100) -> list[MemoryIndexOutboxEvent]:
        now = datetime.now(timezone.utc)
        claimed = []
        with self._lock:
            for event in sorted(self.outbox.values(), key=lambda value: value.created_at or now):
                if len(claimed) >= max(1, int(limit)):
                    break
                if event.status not in {"pending", "retry"} or event.next_attempt_at > now:
                    continue
                updated = replace(event, status="processing", updated_at=now)
                self.outbox[event.id] = updated
                claimed.append(updated)
        return claimed

    def complete_index_event(self, event_id: str) -> None:
        with self._lock:
            event = self.outbox[event_id]
            self.outbox[event_id] = replace(
                event,
                status="completed",
                updated_at=datetime.now(timezone.utc),
            )

    def retry_index_event(
        self,
        event_id: str,
        *,
        error: str,
        next_attempt_at: datetime,
    ) -> None:
        with self._lock:
            event = self.outbox[event_id]
            self.outbox[event_id] = replace(
                event,
                status="retry",
                attempt_count=event.attempt_count + 1,
                next_attempt_at=next_attempt_at,
                last_error=str(error)[:1000],
                updated_at=datetime.now(timezone.utc),
            )

    def _schedule(self, item: MemoryItem, operation: MemoryIndexOperation) -> None:
        now = datetime.now(timezone.utc)
        event_id = str(uuid4())
        self.outbox[event_id] = MemoryIndexOutboxEvent(
            id=event_id,
            memory_id=item.id,
            memory_version=item.version,
            operation=operation,
            status="pending",
            attempt_count=0,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
