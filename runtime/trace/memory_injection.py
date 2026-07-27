from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class MemoryInjectionCandidateTrace:
    memory_id: str
    source: str
    scope: str
    retrieval_score: float
    confidence: float
    selected: bool
    decision_reason: str
    injected_representation: str = "none"
    token_estimate: int = 0
    link_expansion_depth: int = 0
    policy_tags: tuple[str, ...] = ()
    content_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["policy_tags"] = list(self.policy_tags)
        return data


@dataclass(frozen=True)
class MemoryRetrievalTrace:
    query_digest: str
    degraded: bool
    retrieved_count: int
    drop_reasons: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MemoryInjectionTrace:
    retrieval: MemoryRetrievalTrace
    candidates: tuple[MemoryInjectionCandidateTrace, ...]
    pressure_level: str = "unknown"
    link_expansion_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "retrieval": self.retrieval.to_dict(),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "pressure_level": self.pressure_level,
            "link_expansion_used": self.link_expansion_used,
            "selected_count": sum(1 for item in self.candidates if item.selected),
            "filtered_count": sum(1 for item in self.candidates if not item.selected),
        }


def content_digest(content: str) -> str:
    return hashlib.sha256(str(content or "").encode()).hexdigest()[:16]


def query_digest(query: str) -> str:
    return hashlib.sha256(str(query or "").strip().encode()).hexdigest()[:16]


def build_memory_injection_trace(
    *, query: str, degraded: bool, drop_reasons: dict[str, int],
    candidates: Iterable[MemoryInjectionCandidateTrace],
) -> MemoryInjectionTrace:
    values = tuple(candidates)
    return MemoryInjectionTrace(
        retrieval=MemoryRetrievalTrace(
            query_digest=query_digest(query),
            degraded=bool(degraded),
            retrieved_count=len(values),
            drop_reasons=dict(drop_reasons),
        ),
        candidates=values,
    )
