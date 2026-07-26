from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from memory.commands import MemoryTransition
from memory.domain import (
    MemoryEvidence,
    MemoryIndexOperation,
    MemoryItem,
    MemoryKind,
    MemoryOwnerScope,
    MemorySourceType,
    MemoryStatus,
    OwnerKey,
)
from memory.postgres_repository import PostgresMemoryRepository
from memory.repository import InvalidTransition, VersionConflict
from tests.postgres_utils import temporary_postgres_schema


NOW = datetime.now(timezone.utc)


def make_item(
    memory_id: str | None = None,
    *,
    owner_id: str = "alice",
    content: str = "Use concise answers",
    normalized: str = "use concise answers",
    status: MemoryStatus = MemoryStatus.ACTIVE,
    supersedes_id: str | None = None,
    version: int = 1,
) -> MemoryItem:
    return MemoryItem(
        id=memory_id or str(uuid4()),
        owner_scope=MemoryOwnerScope.USER,
        owner_id=owner_id,
        kind=MemoryKind.PREFERENCE,
        content=content,
        normalized_content=normalized,
        status=status,
        confidence=0.9,
        salience=0.8,
        valid_from=NOW - timedelta(minutes=1),
        supersedes_id=supersedes_id,
        version=version,
        created_at=NOW,
        updated_at=NOW,
    )


def make_evidence(memory_id: str, evidence_id: str | None = None) -> MemoryEvidence:
    return MemoryEvidence(
        id=evidence_id or str(uuid4()),
        memory_id=memory_id,
        source_type=MemorySourceType.EXPLICIT_USER,
        source_ref="web:alice:a:1",
        session_id="web:alice:a",
        excerpt="remember this",
        created_at=NOW,
    )


def test_crud_query_and_evidence_round_trip() -> None:
    with temporary_postgres_schema("semantic_memory_crud") as dsn:
        repository = PostgresMemoryRepository(dsn)
        item = make_item()
        evidence = make_evidence(item.id)

        repository.create(item, [evidence])

        assert repository.get(item.id) == item
        assert repository.get_many(["missing", item.id]) == [item]
        assert repository.find_exact(
            item.owner,
            item.kind,
            item.normalized_content,
        ) == item
        assert repository.list_active([item.owner], NOW) == [item]
        assert repository.list_active(
            [OwnerKey(MemoryOwnerScope.USER, "bob")],
            NOW,
        ) == []
        assert repository.list_evidence(item.id) == [evidence]


def test_candidate_is_not_active_and_does_not_schedule_index() -> None:
    with temporary_postgres_schema("semantic_memory_candidate") as dsn:
        repository = PostgresMemoryRepository(dsn)
        candidate = make_item(status=MemoryStatus.CANDIDATE)

        repository.create(candidate)

        assert repository.list_active([candidate.owner], NOW) == []
        assert repository.list_index_events() == []


def test_transition_uses_optimistic_version_and_outbox() -> None:
    with temporary_postgres_schema("semantic_memory_version") as dsn:
        repository = PostgresMemoryRepository(dsn)
        candidate = make_item(status=MemoryStatus.CANDIDATE)
        repository.create(candidate)

        active = repository.transition(MemoryTransition(
            memory_id=candidate.id,
            target_status=MemoryStatus.ACTIVE,
            expected_version=1,
            reason="confirmed",
        ))

        assert active.version == 2
        assert active.status is MemoryStatus.ACTIVE
        events = repository.list_index_events()
        assert len(events) == 1
        assert events[0].operation is MemoryIndexOperation.UPSERT
        with pytest.raises(VersionConflict):
            repository.transition(MemoryTransition(
                memory_id=candidate.id,
                target_status=MemoryStatus.REVOKED,
                expected_version=1,
            ))


def test_supersede_is_atomic_and_preserves_version_chain() -> None:
    with temporary_postgres_schema("semantic_memory_supersede") as dsn:
        repository = PostgresMemoryRepository(dsn)
        old = make_item()
        repository.create(old)
        new = make_item(
            content="Use detailed answers",
            normalized="use detailed answers",
            supersedes_id=old.id,
            version=old.version + 1,
        )

        repository.supersede(
            old.id,
            new,
            [make_evidence(new.id)],
            expected_version=old.version,
        )

        stored_old = repository.get(old.id)
        assert stored_old is not None
        assert stored_old.status is MemoryStatus.SUPERSEDED
        assert repository.get(new.id) == new
        assert repository.list_active([old.owner], NOW) == [new]
        operations = [event.operation for event in repository.list_index_events()]
        assert operations.count(MemoryIndexOperation.UPSERT) == 2
        assert operations.count(MemoryIndexOperation.DELETE) == 1


def test_add_evidence_is_idempotent() -> None:
    with temporary_postgres_schema("semantic_memory_evidence") as dsn:
        repository = PostgresMemoryRepository(dsn)
        item = make_item(status=MemoryStatus.CANDIDATE)
        evidence = make_evidence(item.id)
        repository.create(item, [evidence])

        repository.add_evidence(item.id, [evidence], expected_version=item.version)

        assert repository.list_evidence(item.id) == [evidence]


def test_outbox_claim_complete_and_retry() -> None:
    with temporary_postgres_schema("semantic_memory_outbox") as dsn:
        repository = PostgresMemoryRepository(dsn)
        first = make_item()
        second = make_item()
        repository.create(first)
        repository.create(second)

        claimed = repository.claim_index_events(limit=2)
        assert len(claimed) == 2
        repository.complete_index_event(claimed[0].id)
        repository.retry_index_event(
            claimed[1].id,
            error="qdrant unavailable",
            next_attempt_at=NOW + timedelta(minutes=1),
        )

        statuses = {event.id: event for event in repository.list_index_events()}
        assert statuses[claimed[0].id].status == "completed"
        assert statuses[claimed[1].id].status == "retry"
        assert statuses[claimed[1].id].attempt_count == 1
        assert statuses[claimed[1].id].last_error == "qdrant unavailable"


def test_invalid_transition_rolls_back_without_outbox() -> None:
    with temporary_postgres_schema("semantic_memory_transaction") as dsn:
        repository = PostgresMemoryRepository(dsn)
        item = make_item(status=MemoryStatus.REJECTED)
        repository.create(item)

        with pytest.raises(InvalidTransition):
            repository.transition(MemoryTransition(
                memory_id=item.id,
                target_status=MemoryStatus.ACTIVE,
                expected_version=1,
            ))

        assert repository.get(item.id) == item
        assert repository.list_index_events() == []
