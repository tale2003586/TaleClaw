from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

from memory.domain import MemoryKind, MemoryOwnerScope
from memory.evolution import (
    EvolutionProposedAction,
    EvolutionRelationType,
    RelationDecider,
    RelatedMemoryCandidate,
)
from memory.notes import MemoryNote, MemoryNoteStatus


NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


def _note(note_id: str, content: str, **kwargs) -> MemoryNote:
    return MemoryNote(
        id=note_id,
        content=content,
        memory_type=MemoryKind.PREFERENCE,
        scope=MemoryOwnerScope.USER,
        scope_id="alice",
        status=MemoryNoteStatus.STABLE,
        created_at=NOW,
        updated_at=NOW,
        **kwargs,
    )


def test_exact_duplicate_creates_pending_proposal_without_mutating_target() -> None:
    candidate = _note("candidate", "Prefer concise answers")
    target = _note("stable", "  Prefer concise answers  ")
    before = deepcopy(target)

    proposals = RelationDecider(clock=lambda: NOW).propose(
        candidate,
        [RelatedMemoryCandidate(target, 0.94)],
    )

    assert len(proposals) == 1
    assert proposals[0].relation_type is EvolutionRelationType.DUPLICATE
    assert proposals[0].proposed_action is EvolutionProposedAction.MARK_DUPLICATE
    assert proposals[0].audit_metadata["auto_apply"] is False
    assert target == before


def test_explicit_contradiction_and_supersede_hints_require_confirmation() -> None:
    old = _note("old", "Use tabs")
    candidate = _note(
        "candidate",
        "Use spaces",
        audit_metadata={"relation_hints": {"old": "contradicts"}},
    )
    proposal = RelationDecider(clock=lambda: NOW).propose(
        candidate,
        [RelatedMemoryCandidate(old, 0.88)],
    )[0]
    assert proposal.relation_type is EvolutionRelationType.CONTRADICTS
    assert proposal.proposed_action is EvolutionProposedAction.REQUEST_CONFIRMATION

    superseding = replace(
        candidate,
        audit_metadata={"relation_hints": {"old": "supersedes"}},
    )
    proposal = RelationDecider(clock=lambda: NOW).propose(
        superseding,
        [RelatedMemoryCandidate(old, 0.9)],
    )[0]
    assert proposal.proposed_action is EvolutionProposedAction.ARCHIVE_OLD_AFTER_CONFIRMATION


def test_similarity_only_produces_related_candidate_above_threshold() -> None:
    proposals = RelationDecider(clock=lambda: NOW).propose(
        _note("new", "Python formatting preference"),
        [RelatedMemoryCandidate(_note("old", "Code formatting preference"), 0.8)],
    )
    assert proposals[0].relation_type is EvolutionRelationType.RELATED


def test_low_similarity_produces_no_proposal() -> None:
    proposals = RelationDecider(clock=lambda: NOW).propose(
        _note("new", "Python formatting preference"),
        [RelatedMemoryCandidate(_note("old", "Travel plans"), 0.2)],
    )
    assert proposals == ()


def test_invalid_model_output_falls_back_to_related_or_no_action() -> None:
    class InvalidAdapter:
        def classify(self, candidate, related):
            return "delete_everything", 2.0, "invalid"

    related = RelatedMemoryCandidate(_note("old", "Related preference"), 0.8)
    proposal = RelationDecider(
        model_adapter=InvalidAdapter(),
        clock=lambda: NOW,
    ).propose(_note("new", "New preference"), [related])[0]
    assert proposal.relation_type is EvolutionRelationType.RELATED


def test_proposal_is_serializable_and_id_is_deterministic() -> None:
    decider = RelationDecider(clock=lambda: NOW)
    candidate = _note("new", "same")
    related = RelatedMemoryCandidate(_note("old", "same"), 1.0)
    first = decider.propose(candidate, [related])[0]
    second = decider.propose(candidate, [related])[0]
    assert first.proposal_id == second.proposal_id
    assert first.to_dict()["created_at"] == NOW.isoformat()
    assert first.to_dict()["status"] == "pending"
