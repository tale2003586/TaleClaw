from datetime import datetime, timedelta, timezone

import pytest

from memory.command_service import MemoryCommandService
from memory.commands import MemoryContext, MemoryWriteProposal
from memory.domain import (
    MemoryEvidence,
    MemoryKind,
    MemoryOwnerScope,
    MemorySourceType,
    MemoryStatus,
)
from memory.repository import ScopeDenied
from tests.fakes.in_memory_memory_repository import InMemoryMemoryRepository
from memory.postgres_repository import PostgresMemoryRepository
from memory.index_sync import MemoryIndexSynchronizer
from memory.semantic_retrieval import SemanticMemoryRetrievalService
from tests.fakes.in_memory_memory_index import InMemoryMemoryIndex
from tests.postgres_utils import temporary_postgres_schema


NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)


def context(**overrides) -> MemoryContext:
    values = {
        "user_id": "alice",
        "session_id": "web:alice:a",
        "project_id": "project-a",
        "workspace_id": "workspace-a",
    }
    values.update(overrides)
    return MemoryContext(**values)


def proposal(
    content: str = "Use concise answers",
    *,
    explicit: bool = True,
    owner_scope: MemoryOwnerScope = MemoryOwnerScope.USER,
    owner_id: str = "alice",
) -> MemoryWriteProposal:
    evidence = MemoryEvidence(
        id=f"ev-{abs(hash((content, explicit))) % 100000}",
        memory_id="pending",
        source_type=(MemorySourceType.EXPLICIT_USER if explicit else MemorySourceType.INFERRED),
        source_ref="web:alice:a:1",
        session_id="web:alice:a",
        excerpt=content,
        created_at=NOW,
    )
    return MemoryWriteProposal(
        content=content,
        kind=MemoryKind.PREFERENCE,
        owner_scope=owner_scope,
        owner_id=owner_id,
        source_type=evidence.source_type,
        evidence=(evidence,),
        confidence=0.9,
        salience=0.8,
        explicit_user_request=explicit,
    )


def service():
    repository = InMemoryMemoryRepository()
    events = []
    commands = MemoryCommandService(
        repository,
        trace=lambda event, payload: events.append((event, payload)),
        clock=lambda: NOW,
    )
    return commands, repository, events


def test_remember_creates_active_with_outbox_and_trace() -> None:
    commands, repository, events = service()

    item = commands.remember(proposal(), context())

    assert item.status is MemoryStatus.ACTIVE
    assert repository.get(item.id) == item
    assert len(repository.list_evidence(item.id)) == 1
    assert len(repository.outbox) == 1
    assert events[0][0] == "memory.item.created"
    assert "content_digest" in events[0][1]


def test_propose_never_creates_active() -> None:
    commands, repository, events = service()

    item = commands.propose(proposal(explicit=False), context())

    assert item.status is MemoryStatus.CANDIDATE
    assert repository.outbox == {}
    assert events[0][0] == "memory.candidate.created"


def test_only_verified_coding_conclusions_can_be_recorded_active() -> None:
    commands, repository, _ = service()
    evidence = MemoryEvidence(
        id="ev-code",
        memory_id="pending",
        source_type=MemorySourceType.CODING_CONCLUSION,
        source_ref="task:coding-a/tests/test_api.py",
        task_id="coding-a",
        workspace_id="workspace-a",
        project_id="project-a",
        metadata={"verified": True},
        created_at=NOW,
    )
    coding = MemoryWriteProposal(
        content="API tests use pytest fixtures.",
        kind=MemoryKind.FACT,
        owner_scope=MemoryOwnerScope.PROJECT,
        owner_id="project-a",
        source_type=MemorySourceType.CODING_CONCLUSION,
        evidence=(evidence,),
        confidence=0.95,
    )

    item = commands.record_verified_conclusion(coding, context())

    assert item.status is MemoryStatus.ACTIVE
    assert repository.get(item.id) == item
    with pytest.raises(ValueError, match="verified evidence"):
        commands.record_verified_conclusion(
            MemoryWriteProposal(
                **{
                    **coding.__dict__,
                    "content": "Unverified claim",
                    "evidence": (
                        MemoryEvidence(
                            **{**evidence.__dict__, "id": "ev-unverified", "metadata": {}}
                        ),
                    ),
                }
            ),
            context(),
        )


def test_exact_duplicate_merges_evidence_without_second_item() -> None:
    commands, repository, events = service()
    first = commands.remember(proposal(), context())
    duplicate = proposal()
    duplicate = MemoryWriteProposal(
        **{**duplicate.__dict__, "evidence": (
            MemoryEvidence(
                id="ev-second",
                memory_id="pending",
                source_type=MemorySourceType.EXPLICIT_USER,
                source_ref="web:alice:b:1",
                session_id="web:alice:b",
                created_at=NOW,
            ),
        )}
    )

    result = commands.remember(duplicate, context(session_id="web:alice:b"))

    assert result.id == first.id
    assert len(repository.items) == 1
    assert len(repository.list_evidence(first.id)) == 2
    assert events[-1][0] == "memory.item.duplicate"


def test_confirm_reject_update_and_supersede() -> None:
    commands, repository, _ = service()
    candidate = commands.propose(proposal(explicit=False), context())
    active = commands.confirm(candidate.id, context())
    updated = commands.update(active.id, "Use detailed answers", context())

    assert active.status is MemoryStatus.ACTIVE
    assert repository.get(active.id).status is MemoryStatus.SUPERSEDED
    assert updated.supersedes_id == active.id
    assert updated.version == active.version + 1
    assert repository.list_active(context().allowed_owners(), NOW) == [updated]

    rejected_candidate = commands.propose(
        proposal("I might like tables", explicit=False),
        context(),
    )
    rejected = commands.reject(rejected_candidate.id, "not durable", context())
    assert rejected.status is MemoryStatus.REJECTED


def test_revoke_and_forget_only_owned_active_memory() -> None:
    commands, repository, _ = service()
    concise = commands.remember(proposal(), context())
    commands.remember(proposal("Use pytest", owner_id="bob"), context(user_id="bob"))

    revoked = commands.forget("concise", context())

    assert [item.id for item in revoked] == [concise.id]
    assert repository.get(concise.id).status is MemoryStatus.REVOKED
    assert len(repository.list_active(context(user_id="bob").allowed_owners(), NOW)) == 1


def test_scope_is_taken_from_trusted_context() -> None:
    commands, _, _ = service()

    with pytest.raises(ScopeDenied):
        commands.remember(
            proposal(
                owner_scope=MemoryOwnerScope.PROJECT,
                owner_id="project-b",
            ),
            context(project_id="project-a"),
        )


def test_update_rejects_non_active_memory() -> None:
    commands, _, _ = service()
    candidate = commands.propose(proposal(explicit=False), context())

    with pytest.raises(ValueError, match="Only active"):
        commands.update(candidate.id, "new value", context())


def test_postgres_explicit_memory_vertical_slice_crosses_session_boundary() -> None:
    with temporary_postgres_schema("semantic_vertical_slice") as dsn:
        repository = PostgresMemoryRepository(dsn)
        index = InMemoryMemoryIndex()
        commands = MemoryCommandService(repository, clock=lambda: NOW)
        session_a = context()
        created = commands.remember(proposal(), session_a)

        sync = MemoryIndexSynchronizer(repository, index, clock=lambda: NOW).drain()
        session_b = context(session_id="web:alice:b")
        result = SemanticMemoryRetrievalService(
            repository,
            index,
            clock=lambda: NOW,
        ).retrieve("concise answers", session_b)

        assert created.status is MemoryStatus.ACTIVE
        assert sync.completed == 1
        assert [hit.item.id for hit in result.hits] == [created.id]


def test_postgres_fact_commit_survives_index_failure() -> None:
    with temporary_postgres_schema("semantic_vertical_failure") as dsn:
        repository = PostgresMemoryRepository(dsn)
        commands = MemoryCommandService(repository, clock=lambda: NOW)
        created = commands.remember(proposal(), context())
        index = InMemoryMemoryIndex()
        index.fail_upsert = True

        result = MemoryIndexSynchronizer(
            repository,
            index,
            clock=lambda: NOW,
        ).drain()

        assert result.retried == 1
        assert repository.get(created.id).status is MemoryStatus.ACTIVE
        assert repository.list_index_events(status="retry")
