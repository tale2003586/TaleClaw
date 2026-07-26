from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from memory.commands import MemoryContext
from memory.dedup import normalize_memory_text
from memory.domain import MemoryItem


@dataclass(frozen=True)
class SemanticMemoryHit:
    item: MemoryItem
    relevance: float
    score: float


@dataclass(frozen=True)
class SemanticMemoryResult:
    hits: tuple[SemanticMemoryHit, ...] = ()
    degraded: bool = False
    drop_reasons: dict[str, int] = field(default_factory=dict)


class SemanticMemoryRetrievalService:
    def __init__(
        self,
        repository,
        index,
        *,
        top_k: int = 8,
        trace: Callable[..., None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.index = index
        self.top_k = max(1, int(top_k))
        self.trace = trace
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def retrieve(
        self,
        query: str,
        context: MemoryContext,
        *,
        top_k: int | None = None,
    ) -> SemanticMemoryResult:
        limit = max(1, int(top_k or self.top_k))
        now = self.clock()
        owners = context.allowed_owners()
        drops: dict[str, int] = {}
        degraded = False
        try:
            indexed = self.index.search(query, owners, limit * 3)
        except Exception:
            indexed = []
            degraded = True
        scored: list[SemanticMemoryHit] = []
        if degraded:
            for item in self.repository.list_active(owners, now):
                relevance = _lexical_relevance(query, item.content)
                if relevance <= 0:
                    continue
                scored.append(SemanticMemoryHit(
                    item=item,
                    relevance=relevance,
                    score=_rank(item, relevance, now),
                ))
        else:
            items = {item.id: item for item in self.repository.get_many(
                [hit.memory_id for hit in indexed]
            )}
            allowed = set(owners)
            seen = set()
            for hit in indexed:
                item = items.get(hit.memory_id)
                reason = ""
                if item is None:
                    reason = "missing_source"
                elif item.owner not in allowed:
                    reason = "scope_mismatch"
                elif not item.is_retrievable(now):
                    reason = "inactive_or_expired"
                elif item.version != hit.memory_version:
                    reason = "stale_version"
                elif item.id in seen:
                    reason = "duplicate"
                if reason:
                    drops[reason] = drops.get(reason, 0) + 1
                    continue
                seen.add(item.id)
                relevance = max(0.0, min(1.0, float(hit.score)))
                scored.append(SemanticMemoryHit(
                    item=item,
                    relevance=relevance,
                    score=_rank(item, relevance, now),
                ))
        scored.sort(key=lambda value: (value.score, value.item.updated_at), reverse=True)
        result = SemanticMemoryResult(tuple(scored[:limit]), degraded, drops)
        self._emit(query, context, result)
        return result

    def render(self, result: SemanticMemoryResult) -> str:
        if not result.hits:
            return ""
        lines = ["<semantic_memory>"]
        for hit in result.hits:
            lines.append(
                f'- id="{hit.item.id}" kind="{hit.item.kind.value}" '
                f'scope="{hit.item.owner_scope.value}:{hit.item.owner_id}" '
                f'score="{hit.score:.4f}": {hit.item.content}'
            )
        lines.append("</semantic_memory>")
        return "\n".join(lines)

    def _emit(self, query: str, context: MemoryContext, result: SemanticMemoryResult) -> None:
        if self.trace is None:
            return
        payload = {
            "query_digest": normalize_memory_text(query)[:80],
            "session_id": context.session_id,
            "hit_count": len(result.hits),
            "degraded": result.degraded,
            "drop_reasons": dict(result.drop_reasons),
            "memory_ids": [hit.item.id for hit in result.hits],
            "scores": [hit.score for hit in result.hits],
        }
        try:
            self.trace("memory.semantic.retrieved", payload)
        except TypeError:
            self.trace({"event": "memory.semantic.retrieved", "payload": payload})


def _rank(item: MemoryItem, relevance: float, now: datetime) -> float:
    age_days = max(0.0, (now - item.updated_at).total_seconds() / 86400.0)
    freshness = math.exp(-age_days / 180.0)
    return (
        relevance * 0.5
        + item.confidence * 0.2
        + item.salience * 0.2
        + freshness * 0.1
    )


def _lexical_relevance(query: str, content: str) -> float:
    left = set(normalize_memory_text(query))
    right = set(normalize_memory_text(content))
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))
