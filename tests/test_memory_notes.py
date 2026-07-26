from datetime import datetime, timezone

import pytest

from memory.domain import MemoryItem, MemoryKind, MemoryOwnerScope, MemoryStatus
from memory.notes import (
    MemoryLink,
    MemoryLinkStatus,
    MemoryNote,
    MemoryNoteStatus,
    MemoryRelationType,
)


NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


def note(**overrides):
    values = dict(
        id="note-1", content="User prefers concise answers",
        memory_type=MemoryKind.PREFERENCE, scope=MemoryOwnerScope.USER,
        scope_id="alice", created_at=NOW, updated_at=NOW,
    )
    values.update(overrides)
    return MemoryNote(**values)


def test_note_defaults_are_isolated_and_serializable():
    first = note()
    second = note(id="note-2")
    first.source["x"] = 1

    assert second.source == {}
    assert first.to_dict()["scope"] == "user"
    assert first.to_dict()["created_at"] == NOW.isoformat()


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_note_rejects_invalid_confidence(confidence):
    with pytest.raises(ValueError, match="confidence"):
        note(confidence=confidence)


def test_legacy_round_trip_preserves_identity_scope_state_and_unknown_metadata():
    item = MemoryItem(
        id="memory-1", owner_scope=MemoryOwnerScope.PROJECT, owner_id="project-a",
        kind=MemoryKind.FACT, content="uses postgres", normalized_content="uses postgres",
        status=MemoryStatus.ACTIVE, confidence=.9, salience=.8, valid_from=NOW,
        last_confirmed_at=NOW, created_at=NOW, updated_at=NOW,
        metadata={"unknown": {"keep": True}, "tags": ["db"]},
    )

    projected = MemoryNote.from_legacy(item)
    restored = projected.to_legacy()

    assert projected.status is MemoryNoteStatus.STABLE
    assert restored.id == item.id
    assert restored.owner == item.owner
    assert restored.status is MemoryStatus.ACTIVE
    assert restored.metadata["unknown"] == {"keep": True}
    assert restored.metadata["tags"] == ["db"]


def test_link_distinguishes_candidate_and_accepted():
    candidate = MemoryLink(
        source_memory_id="a", target_memory_id="b",
        relation_type=MemoryRelationType.RELATED_TO,
        confidence=.7, reason="same project",
    )
    accepted = MemoryLink(
        source_memory_id="a", target_memory_id="b",
        relation_type=MemoryRelationType.SUPPORTS,
        confidence=.9, reason="confirmed", status=MemoryLinkStatus.ACCEPTED,
    )

    assert candidate.status is MemoryLinkStatus.CANDIDATE
    assert accepted.status is MemoryLinkStatus.ACCEPTED
    assert candidate.to_dict()["relation_type"] == "related_to"


def test_link_rejects_self_relation_and_invalid_confidence():
    with pytest.raises(ValueError, match="distinct"):
        MemoryLink("a", "a", MemoryRelationType.RELATED_TO, .5, "bad")
    with pytest.raises(ValueError, match="confidence"):
        MemoryLink("a", "b", MemoryRelationType.RELATED_TO, 2, "bad")
