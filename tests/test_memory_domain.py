from datetime import datetime, timedelta, timezone

import pytest

from memory.commands import MemoryContext, MemoryWriteProposal
from memory.domain import (
    MemoryEvidence,
    MemoryKind,
    MemoryOwnerScope,
    MemorySourceType,
    MemoryStatus,
    OwnerKey,
)


NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)


def item(**overrides):
    from memory.domain import MemoryItem

    values = {
        "id": "mem-1",
        "owner_scope": MemoryOwnerScope.USER,
        "owner_id": "alice",
        "kind": MemoryKind.PREFERENCE,
        "content": "Use concise answers",
        "normalized_content": "use concise answers",
        "status": MemoryStatus.CANDIDATE,
        "confidence": 0.8,
        "salience": 0.7,
        "valid_from": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return MemoryItem(**values)


def test_enums_reject_free_form_values() -> None:
    with pytest.raises(ValueError):
        MemoryKind("random")
    with pytest.raises(ValueError):
        MemoryStatus("deleted")


def test_owner_requires_controlled_scope_and_nonempty_id() -> None:
    assert OwnerKey("user", "alice") == OwnerKey(MemoryOwnerScope.USER, "alice")
    with pytest.raises(ValueError):
        OwnerKey(MemoryOwnerScope.USER, "")


def test_memory_item_validates_score_time_and_version() -> None:
    with pytest.raises(ValueError):
        item(confidence=1.1)
    with pytest.raises(ValueError):
        item(valid_until=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError):
        item(version=0)


def test_candidate_can_become_active_then_terminal() -> None:
    active = item().transitioned(MemoryStatus.ACTIVE, now=NOW + timedelta(seconds=1))
    assert active.status is MemoryStatus.ACTIVE
    assert active.version == 2
    revoked = active.transitioned(MemoryStatus.REVOKED, now=NOW + timedelta(seconds=2))
    assert revoked.status is MemoryStatus.REVOKED
    assert not revoked.is_retrievable(NOW + timedelta(seconds=3))


def test_invalid_terminal_transition_is_rejected() -> None:
    rejected = item().transitioned(MemoryStatus.REJECTED)
    with pytest.raises(ValueError, match="Invalid memory transition"):
        rejected.transitioned(MemoryStatus.ACTIVE)


def test_context_builds_only_trusted_available_owners() -> None:
    context = MemoryContext(
        user_id="alice",
        session_id="web:alice:a",
        workspace_id="workspace-a",
        project_id="project-a",
    )
    assert context.permits(OwnerKey(MemoryOwnerScope.USER, "alice"))
    assert context.permits(OwnerKey(MemoryOwnerScope.PROJECT, "project-a"))
    assert not context.permits(OwnerKey(MemoryOwnerScope.PROJECT, "project-b"))


def test_context_requires_user_and_session() -> None:
    with pytest.raises(ValueError):
        MemoryContext(user_id="", session_id="session")
    with pytest.raises(ValueError):
        MemoryContext(user_id="alice", session_id="")


def test_proposal_keeps_controlled_owner_kind_and_evidence() -> None:
    evidence = MemoryEvidence(
        id="ev-1",
        memory_id="pending",
        source_type=MemorySourceType.EXPLICIT_USER,
        source_ref="web:alice:a:1",
        session_id="web:alice:a",
        excerpt="please remember",
    )
    proposal = MemoryWriteProposal(
        content="Use concise answers",
        kind="preference",
        owner_scope="user",
        owner_id="alice",
        source_type="explicit_user",
        evidence=(evidence,),
        explicit_user_request=True,
    )
    assert proposal.owner == OwnerKey(MemoryOwnerScope.USER, "alice")
    assert proposal.kind is MemoryKind.PREFERENCE
    assert proposal.evidence == (evidence,)
