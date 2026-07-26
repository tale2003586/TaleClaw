from datetime import datetime, timezone

from memory.commands import MemoryWriteProposal
from memory.conflict_service import ConflictAction, MemoryConflictService
from memory.domain import (
    MemoryItem,
    MemoryKind,
    MemoryOwnerScope,
    MemorySourceType,
    MemoryStatus,
)


NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)


def stored(memory_id: str, *, owner_id: str = "alice", content: str = "Use pytest fixtures"):
    from memory.dedup import normalize_memory_text
    return MemoryItem(
        id=memory_id,
        owner_scope=MemoryOwnerScope.PROJECT,
        owner_id=owner_id,
        kind=MemoryKind.PROCEDURE,
        content=content,
        normalized_content=normalize_memory_text(content),
        status=MemoryStatus.ACTIVE,
        confidence=0.8,
        salience=0.7,
        valid_from=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def proposal(content: str, *, owner_id: str = "alice", metadata=None):
    return MemoryWriteProposal(
        content=content,
        kind=MemoryKind.PROCEDURE,
        owner_scope=MemoryOwnerScope.PROJECT,
        owner_id=owner_id,
        source_type=MemorySourceType.EXPLICIT_USER,
        explicit_user_request=True,
        metadata=metadata or {},
    )


def test_exact_duplicate_merges_only_in_same_scope_and_kind() -> None:
    service = MemoryConflictService()
    existing = stored("mem-a")

    same = service.decide(proposal("Use pytest fixtures"), [existing])
    other_scope = service.decide(proposal("Use pytest fixtures", owner_id="bob"), [existing])

    assert same.action is ConflictAction.MERGE_EXACT
    assert same.existing == existing
    assert other_scope.action is ConflictAction.CREATE


def test_semantic_duplicate_merges_evidence() -> None:
    service = MemoryConflictService()
    existing = stored("mem-a", content="Project tests use pytest fixture")

    decision = service.decide(
        proposal("Project tests use pytest fixtures"),
        [existing],
    )

    assert decision.action is ConflictAction.MERGE_SEMANTIC


def test_explicit_trusted_conflict_creates_supersede_decision() -> None:
    service = MemoryConflictService()
    existing = stored("mem-a")

    decision = service.decide(
        proposal("Use unittest setup", metadata={"conflicts_with_id": "mem-a"}),
        [existing],
    )

    assert decision.action is ConflictAction.SUPERSEDE
    assert decision.existing == existing


def test_uncertain_semantic_relation_stays_candidate() -> None:
    service = MemoryConflictService(semantic_resolver=lambda old, new: "uncertain")
    existing = stored("mem-a", content="Project tests use pytest fixture")

    decision = service.decide(
        proposal("Project tests use pytest fixtures"),
        [existing],
    )

    assert decision.action is ConflictAction.KEEP_CANDIDATE
