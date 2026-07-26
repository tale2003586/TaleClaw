from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from memory.commands import MemoryContext
from memory.domain import MemorySourceType, MemoryStatus


class PromotionOutcome(StrEnum):
    PROMOTE = "promote"
    KEEP_CANDIDATE = "keep_candidate"
    REQUIRE_CONFIRMATION = "require_confirmation"
    REJECT = "reject"


@dataclass(frozen=True)
class PromotionDecision:
    outcome: PromotionOutcome
    reason: str
    independent_evidence_count: int = 0


class MemoryPromotionService:
    def __init__(
        self,
        command_service,
        repository,
        *,
        min_confidence: float = 0.85,
        min_independent_evidence: int = 2,
    ) -> None:
        self.command_service = command_service
        self.repository = repository
        self.min_confidence = float(min_confidence)
        self.min_independent_evidence = max(2, int(min_independent_evidence))

    def evaluate(self, memory_id: str) -> PromotionDecision:
        item = self.repository.get(memory_id)
        if item is None:
            return PromotionDecision(PromotionOutcome.REJECT, "memory_not_found")
        if item.status is not MemoryStatus.CANDIDATE:
            return PromotionDecision(PromotionOutcome.REJECT, "not_a_candidate")
        evidence = self.repository.list_evidence(memory_id)
        independent = {
            (value.session_id or "", value.task_id or "", value.source_ref or "")
            for value in evidence
            if value.session_id or value.task_id or value.source_ref
        }
        count = len(independent)
        if item.metadata.get("negated") or item.metadata.get("corrected"):
            return PromotionDecision(
                PromotionOutcome.REQUIRE_CONFIRMATION,
                "candidate_has_negation_or_correction",
                count,
            )
        source_types = {value.source_type for value in evidence}
        if MemorySourceType.CODING_CONCLUSION in source_types:
            verified = all(bool(value.metadata.get("verified")) for value in evidence)
            if not verified:
                return PromotionDecision(
                    PromotionOutcome.REQUIRE_CONFIRMATION,
                    "coding_conclusion_not_verified",
                    count,
                )
        if count < self.min_independent_evidence:
            return PromotionDecision(
                PromotionOutcome.KEEP_CANDIDATE,
                "independent_evidence_required",
                count,
            )
        if item.confidence < self.min_confidence:
            return PromotionDecision(
                PromotionOutcome.KEEP_CANDIDATE,
                "confidence_below_threshold",
                count,
            )
        return PromotionDecision(
            PromotionOutcome.PROMOTE,
            "promotion_policy_satisfied",
            count,
        )

    def promote_if_eligible(self, memory_id: str, context: MemoryContext):
        decision = self.evaluate(memory_id)
        if decision.outcome is not PromotionOutcome.PROMOTE:
            return None, decision
        return self.command_service.confirm(memory_id, context), decision
