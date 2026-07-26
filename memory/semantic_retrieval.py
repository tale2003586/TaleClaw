from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from memory.commands import MemoryContext
from memory.dedup import normalize_memory_text
from memory.domain import MemoryItem
from runtime.trace.memory_injection import (
    MemoryInjectionCandidateTrace,
    build_memory_injection_trace,
    content_digest,
)


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
        injection_trace_enabled: bool = False,
    ) -> None:
        self.repository = repository
        self.index = index
        self.top_k = max(1, int(top_k))
        self.trace = trace
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.injection_trace_enabled = bool(injection_trace_enabled)
        self.trace_events: list[dict] = []

    def drain_trace_events(self) -> list[dict]:
        events = list(self.trace_events)
        self.trace_events.clear()
        return events

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
        filtered_decisions: list[MemoryInjectionCandidateTrace] = []
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
                    filtered_decisions.append(_candidate_trace(
                        item,
                        memory_id=hit.memory_id,
                        relevance=float(getattr(hit, "score", 0.0) or 0.0),
                        selected=False,
                        reason=reason,
                    ))
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
        selected_decisions = [
            _candidate_trace(
                hit.item,
                relevance=hit.relevance,
                selected=True,
                reason=(
                    "scope_limited_lexical_fallback"
                    if degraded
                    else "active_current_scope_match"
                ),
            )
            for hit in result.hits
        ]
        self._emit(query, context, result, [*selected_decisions, *filtered_decisions])
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

    def _emit(
        self,
        query: str,
        context: MemoryContext,
        result: SemanticMemoryResult,
        candidate_decisions: list[MemoryInjectionCandidateTrace],
    ) -> None:
        payload = {
            "query_digest": normalize_memory_text(query)[:80],
            "session_id": context.session_id,
            "hit_count": len(result.hits),
            "degraded": result.degraded,
            "drop_reasons": dict(result.drop_reasons),
            "memory_ids": [hit.item.id for hit in result.hits],
            "scores": [hit.score for hit in result.hits],
        }
        self.trace_events.append({"event": "memory.semantic.retrieved", "payload": payload})
        events = [("memory.semantic.retrieved", payload)]
        if self.injection_trace_enabled:
            explanation = build_memory_injection_trace(
                query=query,
                degraded=result.degraded,
                drop_reasons=result.drop_reasons,
                candidates=candidate_decisions,
            ).to_dict()
            self.trace_events.append({
                "event": "memory.injection.explained",
                "payload": explanation,
            })
            events.append(("memory.injection.explained", explanation))
        if self.trace is None:
            return
        for event_name, event_payload in events:
            try:
                self.trace(event_name, event_payload)
            except TypeError:
                try:
                    self.trace({"event": event_name, "payload": event_payload})
                except Exception:
                    pass
            except Exception:
                pass


def _candidate_trace(
    item: MemoryItem | None,
    *,
    memory_id: str = "",
    relevance: float,
    selected: bool,
    reason: str,
) -> MemoryInjectionCandidateTrace:
    content = item.content if item is not None else ""
    return MemoryInjectionCandidateTrace(
        memory_id=item.id if item is not None else str(memory_id),
        source="postgres" if item is not None else "semantic_index",
        scope=(
            f"{item.owner_scope.value}:{item.owner_id}"
            if item is not None
            else "unknown"
        ),
        retrieval_score=max(0.0, min(1.0, float(relevance))),
        confidence=item.confidence if item is not None else 0.0,
        selected=selected,
        decision_reason=reason,
        injected_representation="full_content" if selected else "none",
        token_estimate=max(0, len(content) // 4) if selected else 0,
        policy_tags=("active", "scope_match") if selected else (reason,),
        content_digest=content_digest(content) if content else "",
    )


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
