from datetime import datetime, timezone

from memory.command_service import MemoryCommandService
from memory.commands import MemoryContext, MemoryWriteProposal
from memory.domain import (
    MemoryEvidence,
    MemoryKind,
    MemoryOwnerScope,
    MemorySourceType,
    MemoryStatus,
)
from memory.promotion_service import MemoryPromotionService, PromotionOutcome
from tests.fakes.in_memory_memory_repository import InMemoryMemoryRepository


NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)


def evidence(evidence_id: str, session_id: str, *, verified=True):
    return MemoryEvidence(
        id=evidence_id,
        memory_id="pending",
        source_type=MemorySourceType.INFERRED,
        source_ref=f"{session_id}:1",
        session_id=session_id,
        metadata={"verified": verified},
        created_at=NOW,
    )


def candidate(repository, commands, items, *, confidence=0.9, source=MemorySourceType.INFERRED):
    proposal = MemoryWriteProposal(
        content="Prefer concise answers",
        kind=MemoryKind.PREFERENCE,
        owner_scope=MemoryOwnerScope.USER,
        owner_id="alice",
        source_type=source,
        evidence=tuple(items),
        confidence=confidence,
        salience=0.8,
    )
    return commands.propose(
        proposal,
        MemoryContext(user_id="alice", session_id="web:alice:a"),
    )


def test_single_inference_stays_candidate() -> None:
    repository = InMemoryMemoryRepository()
    commands = MemoryCommandService(repository, clock=lambda: NOW)
    item = candidate(repository, commands, [evidence("ev-a", "web:alice:a")])

    decision = MemoryPromotionService(commands, repository).evaluate(item.id)

    assert item.status is MemoryStatus.CANDIDATE
    assert decision.outcome is PromotionOutcome.KEEP_CANDIDATE
    assert decision.independent_evidence_count == 1


def test_independent_sessions_and_confidence_allow_promotion() -> None:
    repository = InMemoryMemoryRepository()
    commands = MemoryCommandService(repository, clock=lambda: NOW)
    item = candidate(repository, commands, [
        evidence("ev-a", "web:alice:a"),
        evidence("ev-b", "web:alice:b"),
    ])
    service = MemoryPromotionService(commands, repository)

    promoted, decision = service.promote_if_eligible(
        item.id,
        MemoryContext(user_id="alice", session_id="web:alice:b"),
    )

    assert decision.outcome is PromotionOutcome.PROMOTE
    assert promoted.status is MemoryStatus.ACTIVE


def test_low_confidence_and_correction_do_not_auto_promote() -> None:
    repository = InMemoryMemoryRepository()
    commands = MemoryCommandService(repository, clock=lambda: NOW)
    low = candidate(repository, commands, [
        evidence("ev-a", "web:alice:a"),
        evidence("ev-b", "web:alice:b"),
    ], confidence=0.4)
    service = MemoryPromotionService(commands, repository)

    decision = service.evaluate(low.id)

    assert decision.outcome is PromotionOutcome.KEEP_CANDIDATE
    assert decision.reason == "confidence_below_threshold"


def test_unverified_coding_conclusion_requires_confirmation() -> None:
    repository = InMemoryMemoryRepository()
    commands = MemoryCommandService(repository, clock=lambda: NOW)
    coding_evidence = MemoryEvidence(
        id="ev-task",
        memory_id="pending",
        source_type=MemorySourceType.CODING_CONCLUSION,
        source_ref="task:a/file.py:10",
        task_id="task-a",
        workspace_id="workspace-a",
        metadata={"verified": False},
        created_at=NOW,
    )
    item = candidate(
        repository,
        commands,
        [coding_evidence],
        source=MemorySourceType.CODING_CONCLUSION,
    )

    decision = MemoryPromotionService(commands, repository).evaluate(item.id)

    assert decision.outcome is PromotionOutcome.REQUIRE_CONFIRMATION
    assert decision.reason == "coding_conclusion_not_verified"
