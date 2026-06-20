from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    text: str
    scope: str
    source_type: str
    source_ref: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryHit:
    id: str
    text: str
    score: float
    scope: str
    source_type: str
    source_ref: str = ""
    metadata: dict = field(default_factory=dict)


class MemoryVectorIndex(Protocol):
    def upsert(self, record: MemoryRecord) -> None:
        ...

    def search(
        self,
        *,
        query: str,
        scope: str,
        top_k: int,
        min_score: float = 0.0,
    ) -> list[MemoryHit]:
        ...


class NullMemoryVectorIndex:
    def upsert(self, record: MemoryRecord) -> None:
        return None

    def search(
        self,
        *,
        query: str,
        scope: str,
        top_k: int,
        min_score: float = 0.0,
    ) -> list[MemoryHit]:
        return []
