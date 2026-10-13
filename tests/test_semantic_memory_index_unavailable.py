from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from memory.command_service import MemoryCommandService
from memory.commands import MemoryContext, MemoryWriteProposal
from memory.domain import MemoryKind, MemoryOwnerScope, MemorySourceType, MemoryStatus
from memory.index_sync import MemoryIndexSynchronizer
from memory.migration.rebuild_index import RebuildSemanticMemoryIndex
from memory.semantic_index import NullSemanticMemoryIndex
from memory.semantic_retrieval import SemanticMemoryRetrievalService
from memory.vector_runtime import build_semantic_memory_index_from_env
from tests.fakes.in_memory_memory_index import InMemoryMemoryIndex
from tests.fakes.in_memory_memory_repository import InMemoryMemoryRepository


@pytest.fixture(params=["direct", "disabled", "initialization_error"])
def unavailable_index(request, monkeypatch):
    if request.param == "direct":
        return NullSemanticMemoryIndex()
    monkeypatch.setenv("SEMANTIC_MEMORY_INDEX_STRICT", "0")
    monkeypatch.setenv(
        "SEMANTIC_MEMORY_INDEX_ENABLED",
        "0" if request.param == "disabled" else "1",
    )
    with patch(
        "memory.vector_runtime.QdrantSemanticMemoryIndex",
        side_effect=RuntimeError("index backend unavailable"),
    ) as factory:
        index = build_semantic_memory_index_from_env(embeddings=object())
    if request.param == "disabled":
        factory.assert_not_called()
    else:
        factory.assert_called_once()
    assert isinstance(index, NullSemanticMemoryIndex)
    return index


def remember(repository, *, user_id="alice", content="Prefer concise answers"):
    context = MemoryContext(user_id=user_id, session_id=f"web:{user_id}:a")
    item = MemoryCommandService(repository).remember(MemoryWriteProposal(
        content=content,
        kind=MemoryKind.FACT,
        owner_scope=MemoryOwnerScope.USER,
        owner_id=user_id,
        source_type=MemorySourceType.EXPLICIT_USER,
        explicit_user_request=True,
    ), context)
    return item, context


def test_unavailable_index_uses_scope_limited_database_fallback(unavailable_index):
    repository = InMemoryMemoryRepository()
    item, context = remember(repository)
    remember(repository, user_id="bob", content="Concise answers for Bob")

    result = SemanticMemoryRetrievalService(repository, unavailable_index).retrieve(
        "concise", context,
    )

    assert result.degraded
    assert [hit.item.id for hit in result.hits] == [item.id]


@pytest.mark.parametrize("operation", ["upsert", "delete"])
def test_unavailable_index_retries_until_recovery(unavailable_index, operation):
    repository = InMemoryMemoryRepository()
    item, context = remember(repository)
    healthy_index = InMemoryMemoryIndex()
    if operation == "delete":
        MemoryIndexSynchronizer(repository, healthy_index).drain()
        MemoryCommandService(repository).revoke(item.id, "forgotten", context)
    expected_status = MemoryStatus.ACTIVE if operation == "upsert" else MemoryStatus.REVOKED

    failed = MemoryIndexSynchronizer(repository, unavailable_index).drain()

    assert failed.claimed == 1
    assert failed.completed == 0
    assert failed.retried == 1
    event = next(value for value in repository.outbox.values() if value.operation.value == operation)
    assert event.status == "retry"
    assert event.attempt_count == 1
    assert "unavailable" in event.last_error.lower()
    assert event.next_attempt_at > datetime.now(timezone.utc)
    assert repository.get(item.id).status is expected_status

    # Make the fake repository's retry due, as after the existing backoff expires.
    repository.outbox[event.id] = replace(
        event, next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    recovered = MemoryIndexSynchronizer(repository, healthy_index).drain()

    assert recovered.completed == 1
    assert recovered.retried == 0
    assert repository.outbox[event.id].status == "completed"
    assert repository.get(item.id).status is expected_status
    assert bool(healthy_index.items) is (operation == "upsert")


def test_unavailable_index_rebuild_reports_failure(unavailable_index):
    repository = InMemoryMemoryRepository()
    item, _ = remember(repository)

    report = RebuildSemanticMemoryIndex(repository, unavailable_index).rebuild(dry_run=False)

    assert report.selected == 1
    assert report.indexed == 0
    assert report.failed == 1
    assert item.id in report.errors[0]
    assert "unavailable" in report.errors[0].lower()


def test_strict_index_initialization_still_propagates_failure(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MEMORY_INDEX_ENABLED", "1")
    monkeypatch.setenv("SEMANTIC_MEMORY_INDEX_STRICT", "1")
    with patch(
        "memory.vector_runtime.QdrantSemanticMemoryIndex",
        side_effect=RuntimeError("backend initialization failed"),
    ), pytest.raises(RuntimeError, match="backend initialization failed"):
        build_semantic_memory_index_from_env(embeddings=object())
