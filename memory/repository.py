from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence

from memory.commands import MemoryTransition
from memory.domain import (
    MemoryEvidence,
    MemoryIndexOperation,
    MemoryItem,
    MemoryKind,
    OwnerKey,
)


class MemoryRepositoryError(RuntimeError):
    """Base error returned by persistence adapters."""


class MemoryNotFound(MemoryRepositoryError):
    pass


class VersionConflict(MemoryRepositoryError):
    pass


class ScopeDenied(MemoryRepositoryError):
    pass


class InvalidTransition(MemoryRepositoryError):
    pass


@dataclass(frozen=True)
class MemoryIndexOutboxEvent:
    id: str
    memory_id: str
    memory_version: int
    operation: MemoryIndexOperation
    status: str
    attempt_count: int
    next_attempt_at: datetime
    last_error: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MemoryRepository(Protocol):
    def create(
        self,
        item: MemoryItem,
        evidence: Sequence[MemoryEvidence] = (),
    ) -> MemoryItem: ...

    def get(self, memory_id: str) -> MemoryItem | None: ...

    def get_many(self, memory_ids: Sequence[str]) -> list[MemoryItem]: ...

    def list_active(
        self,
        scopes: Sequence[OwnerKey],
        now: datetime,
    ) -> list[MemoryItem]: ...

    def find_exact(
        self,
        owner: OwnerKey,
        kind: MemoryKind,
        normalized: str,
    ) -> MemoryItem | None: ...

    def transition(self, command: MemoryTransition) -> MemoryItem: ...

    def supersede(
        self,
        old_memory_id: str,
        new_item: MemoryItem,
        evidence: Sequence[MemoryEvidence],
        *,
        expected_version: int,
    ) -> MemoryItem: ...

    def add_evidence(
        self,
        memory_id: str,
        evidence: Sequence[MemoryEvidence],
        *,
        expected_version: int | None = None,
    ) -> MemoryItem: ...

    def list_evidence(self, memory_id: str) -> list[MemoryEvidence]: ...

    def claim_index_events(self, limit: int = 100) -> list[MemoryIndexOutboxEvent]: ...

    def complete_index_event(self, event_id: str) -> None: ...

    def retry_index_event(
        self,
        event_id: str,
        *,
        error: str,
        next_attempt_at: datetime,
    ) -> None: ...
