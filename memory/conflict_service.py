from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Sequence

from memory.commands import MemoryWriteProposal
from memory.dedup import is_semantic_duplicate, normalize_memory_text
from memory.domain import MemoryItem, MemoryStatus


class ConflictAction(StrEnum):
    CREATE = "create"
    MERGE_EXACT = "merge_exact"
    MERGE_SEMANTIC = "merge_semantic"
    SUPERSEDE = "supersede"
    KEEP_CANDIDATE = "keep_candidate"


@dataclass(frozen=True)
class ConflictDecision:
    action: ConflictAction
    existing: MemoryItem | None = None
    reason: str = ""


class MemoryConflictService:
    """Deterministic conflict policy with an optional bounded semantic resolver."""

    def __init__(
        self,
        *,
        semantic_resolver: Callable[[str, str], str] | None = None,
    ) -> None:
        self.semantic_resolver = semantic_resolver

    def decide(
        self,
        proposal: MemoryWriteProposal,
        existing: Sequence[MemoryItem],
    ) -> ConflictDecision:
        normalized = normalize_memory_text(proposal.content)
        same_owner_kind = [
            item for item in existing
            if item.owner == proposal.owner
            and item.kind == proposal.kind
            and item.status in {MemoryStatus.CANDIDATE, MemoryStatus.ACTIVE}
        ]
        for item in same_owner_kind:
            if item.normalized_content == normalized:
                return ConflictDecision(
                    ConflictAction.MERGE_EXACT,
                    item,
                    "same owner, kind and normalized content",
                )
        for item in same_owner_kind:
            if not is_semantic_duplicate(proposal.content, item.content):
                continue
            outcome = "duplicate"
            if self.semantic_resolver is not None:
                outcome = str(self.semantic_resolver(item.content, proposal.content)).lower()
            if outcome == "conflict":
                return ConflictDecision(
                    ConflictAction.SUPERSEDE,
                    item,
                    "semantic resolver identified a correction",
                )
            if outcome == "uncertain":
                return ConflictDecision(
                    ConflictAction.KEEP_CANDIDATE,
                    item,
                    "semantic relationship is uncertain",
                )
            return ConflictDecision(
                ConflictAction.MERGE_SEMANTIC,
                item,
                "semantically equivalent content",
            )
        conflicts_with = str(proposal.metadata.get("conflicts_with_id") or "")
        if conflicts_with:
            for item in same_owner_kind:
                if item.id == conflicts_with:
                    return ConflictDecision(
                        ConflictAction.SUPERSEDE,
                        item,
                        "trusted caller identified the corrected memory",
                    )
        return ConflictDecision(ConflictAction.CREATE, reason="no duplicate or conflict")
