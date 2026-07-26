from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Protocol, Sequence

from memory.dedup import normalize_memory_text
from memory.notes import MemoryNote


class EvolutionRelationType(StrEnum):
    DUPLICATE = "duplicate"
    RELATED = "related"
    UPDATES = "updates"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"
    ENRICHES = "enriches"


class EvolutionProposalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class EvolutionProposedAction(StrEnum):
    CREATE_LINK = "create_link"
    MERGE_METADATA = "merge_metadata"
    MARK_DUPLICATE = "mark_duplicate"
    REQUEST_CONFIRMATION = "request_confirmation"
    ARCHIVE_OLD_AFTER_CONFIRMATION = "archive_old_after_confirmation"
    REPLACE_AFTER_CONFIRMATION = "replace_after_confirmation"
    NO_ACTION = "no_action"


class RelationCreatedBy(StrEnum):
    RULE = "rule"
    LLM = "llm"
    RUNTIME = "runtime"
    USER = "user"


@dataclass(frozen=True)
class RelatedMemoryCandidate:
    note: MemoryNote
    similarity_score: float
    retrieval_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        score = float(self.similarity_score)
        if not 0.0 <= score <= 1.0:
            raise ValueError("similarity_score must be between 0 and 1.")
        object.__setattr__(self, "similarity_score", score)
        object.__setattr__(self, "retrieval_metadata", dict(self.retrieval_metadata or {}))


@dataclass(frozen=True)
class MemoryEvolutionProposal:
    proposal_id: str
    candidate_memory_id: str
    target_memory_ids: tuple[str, ...]
    relation_type: EvolutionRelationType
    reason: str
    confidence: float
    proposed_action: EvolutionProposedAction
    created_by: RelationCreatedBy
    created_at: datetime
    status: EvolutionProposalStatus = EvolutionProposalStatus.PENDING
    policy_decision: str = "review_required"
    source: str = "relation_decider"
    audit_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.proposal_id or "").strip():
            raise ValueError("proposal_id is required.")
        candidate_id = str(self.candidate_memory_id or "").strip()
        targets = tuple(dict.fromkeys(str(item).strip() for item in self.target_memory_ids if str(item).strip()))
        if not candidate_id or not targets or candidate_id in targets:
            raise ValueError("proposal requires a candidate and distinct target memories.")
        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1.")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware.")
        object.__setattr__(self, "candidate_memory_id", candidate_id)
        object.__setattr__(self, "target_memory_ids", targets)
        object.__setattr__(self, "relation_type", EvolutionRelationType(self.relation_type))
        object.__setattr__(self, "proposed_action", EvolutionProposedAction(self.proposed_action))
        object.__setattr__(self, "created_by", RelationCreatedBy(self.created_by))
        object.__setattr__(self, "status", EvolutionProposalStatus(self.status))
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "reason", str(self.reason or "")[:500])
        object.__setattr__(self, "audit_metadata", dict(self.audit_metadata or {}))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("relation_type", "proposed_action", "created_by", "status"):
            payload[key] = getattr(self, key).value
        payload["created_at"] = self.created_at.isoformat()
        payload["target_memory_ids"] = list(self.target_memory_ids)
        return payload


class RelationModelAdapter(Protocol):
    def classify(
        self,
        candidate: MemoryNote,
        related: RelatedMemoryCandidate,
    ) -> tuple[str, float, str]: ...


class RelationDecider:
    """Conservative proposal generator. It never applies memory mutations."""

    def __init__(
        self,
        *,
        model_adapter: RelationModelAdapter | None = None,
        related_threshold: float = 0.72,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.model_adapter = model_adapter
        self.related_threshold = float(related_threshold)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def propose(
        self,
        candidate: MemoryNote,
        related_candidates: Sequence[RelatedMemoryCandidate],
        *,
        policy_decision: str = "review_required",
        source: str = "relation_decider",
    ) -> tuple[MemoryEvolutionProposal, ...]:
        proposals = []
        for related in related_candidates:
            if related.note.id == candidate.id:
                continue
            decision = self._classify(candidate, related)
            if decision is None:
                continue
            relation, confidence, reason, created_by = decision
            action = _action_for(relation)
            proposal_id = _proposal_id(candidate.id, related.note.id, relation)
            proposals.append(MemoryEvolutionProposal(
                proposal_id=proposal_id,
                candidate_memory_id=candidate.id,
                target_memory_ids=(related.note.id,),
                relation_type=relation,
                reason=reason,
                confidence=confidence,
                proposed_action=action,
                created_by=created_by,
                created_at=self.clock(),
                policy_decision=policy_decision,
                source=source,
                audit_metadata={
                    "similarity_score": related.similarity_score,
                    "retrieval_metadata": related.retrieval_metadata,
                    "auto_apply": False,
                },
            ))
        return tuple(proposals)

    def _classify(self, candidate, related):
        if normalize_memory_text(candidate.content) == normalize_memory_text(related.note.content):
            return (
                EvolutionRelationType.DUPLICATE,
                max(related.similarity_score, 0.99),
                "normalized content is identical",
                RelationCreatedBy.RULE,
            )
        hint = _relation_hint(candidate, related.note.id)
        if hint is not None:
            return hint, related.similarity_score, "explicit relation hint", RelationCreatedBy.RUNTIME
        if self.model_adapter is not None:
            try:
                raw_relation, raw_confidence, reason = self.model_adapter.classify(candidate, related)
                relation = EvolutionRelationType(raw_relation)
                confidence = min(1.0, max(0.0, float(raw_confidence)))
                if confidence >= 0.5:
                    return relation, confidence, str(reason or "model relation candidate"), RelationCreatedBy.LLM
            except (TypeError, ValueError):
                pass
        if related.similarity_score >= self.related_threshold:
            return (
                EvolutionRelationType.RELATED,
                related.similarity_score,
                "retrieval similarity exceeds related threshold",
                RelationCreatedBy.RULE,
            )
        return None


def _relation_hint(candidate: MemoryNote, target_id: str) -> EvolutionRelationType | None:
    hints = candidate.audit_metadata.get("relation_hints") or {}
    raw = hints.get(target_id) if isinstance(hints, dict) else None
    try:
        return EvolutionRelationType(raw)
    except (TypeError, ValueError):
        return None


def _action_for(relation: EvolutionRelationType) -> EvolutionProposedAction:
    return {
        EvolutionRelationType.DUPLICATE: EvolutionProposedAction.MARK_DUPLICATE,
        EvolutionRelationType.RELATED: EvolutionProposedAction.CREATE_LINK,
        EvolutionRelationType.ENRICHES: EvolutionProposedAction.MERGE_METADATA,
        EvolutionRelationType.UPDATES: EvolutionProposedAction.REQUEST_CONFIRMATION,
        EvolutionRelationType.CONTRADICTS: EvolutionProposedAction.REQUEST_CONFIRMATION,
        EvolutionRelationType.SUPERSEDES: EvolutionProposedAction.ARCHIVE_OLD_AFTER_CONFIRMATION,
    }[relation]


def _proposal_id(candidate_id: str, target_id: str, relation: EvolutionRelationType) -> str:
    digest = hashlib.sha256(f"{candidate_id}:{target_id}:{relation.value}".encode()).hexdigest()[:20]
    return f"evo_{digest}"
