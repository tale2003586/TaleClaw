from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol


class LLMRouteClassifier(Protocol):
    def __call__(self, query: str, *, embedding_score: float, matched_intent: str) -> "RetrievalDecision":
        ...


class QueryRewriteProvider(Protocol):
    def rewrite(self, request: "RewriteRequest") -> "RewriteResult":
        ...


@dataclass(frozen=True)
class SecurityRouteConfig:
    high_threshold: float = 0.5
    low_threshold: float = 0.1
    llm_accept_threshold: float = 0.60
    default_top_k: int = 5
    min_score: float = 0.0
    retrieval_direct_threshold: float = 0.68
    retrieval_medium_threshold: float = 0.48
    retrieval_low_threshold: float = 0.35
    retrieval_gap_threshold: float = 0.08
    retrieval_concentration_threshold: float = 0.67
    retrieval_rerank_min_hits: int = 5
    retrieval_consistency_gate_enabled: bool = True
    retrieval_min_consistency_votes: int = 1
    retrieval_cross_mode_overlap_threshold: float = 0.30
    retrieval_rewrite_overlap_threshold: float = 0.20
    retrieval_reranker_direct_threshold: float = 0.68
    retrieval_intent_concentration_threshold: float = 0.60
    decompose_parallel_enabled: bool = True
    decompose_parallel_workers: int = 4
    pre_dense_rewrite_enabled: bool = False
    pre_dense_rewrite_provider: str = "rule"
    pre_dense_parallel_enabled: bool = True


@dataclass(frozen=True)
class RewriteRequest:
    query: str
    mode: str
    route: str = ""
    tier: str = ""
    matched_intent: str = ""
    embedding_score: float = 0.0
    keyword_matches: list[str] = field(default_factory=list)
    top_score: float = 0.0
    score_gap: float = 0.0
    source_concentration: float = 0.0
    top_hits: list[dict[str, str]] = field(default_factory=list)
    fallback_query: str = ""
    fallback_queries: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RewriteResult:
    query: str
    queries: list[str] = field(default_factory=list)
    reason: str = ""
    provider: str = "rule"
    metadata: dict[str, str] = field(default_factory=dict)
    latency_ms: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalDecision:
    use_rag: bool
    target: str | None
    query: str
    confidence: float
    reason: str
    route: str
    keyword_matches: list[str] = field(default_factory=list)
    embedding_score: float = 0.0
    matched_intent: str = ""
    llm_required: bool = False
    top_k: int = 5
    min_score: float = 0.0
    clarification: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RetrievalSearchRecord:
    stage: str
    query: str
    mode: str
    use_reranker: bool
    hit_count: int
    tier: str = "low"
    top_score: float = 0.0
    score_gap: float = 0.0
    source_concentration: float = 0.0
    intent_concentration: float = 0.0
    cache_hit: bool = False
    candidate_count: int = 0
    final_count: int = 0
    latency_ms: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class RoutedRetrievalPlan:
    decision: RetrievalDecision
    hits: list
    action: str
    reason: str
    searches: list[RetrievalSearchRecord] = field(default_factory=list)
    rewrites: list[RewriteResult] = field(default_factory=list)
    llm_decision: RetrievalDecision | None = None

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.to_dict(),
            "action": self.action,
            "reason": self.reason,
            "hits": [
                {
                    "id": str(getattr(hit, "id", "")),
                    "score": float(getattr(hit, "score", 0.0) or 0.0),
                    "source_relpath": str(getattr(hit, "source_relpath", "")),
                    "title": str(getattr(hit, "title", "")),
                }
                for hit in self.hits
            ],
            "searches": [asdict(record) for record in self.searches],
            "rewrites": [asdict(rewrite) for rewrite in self.rewrites],
            "llm_decision": self.llm_decision.to_dict() if self.llm_decision else None,
        }
