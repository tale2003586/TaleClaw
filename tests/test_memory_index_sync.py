from datetime import datetime, timezone

from memory.command_service import MemoryCommandService
from memory.commands import MemoryContext, MemoryWriteProposal
from memory.domain import MemoryKind, MemoryOwnerScope, MemorySourceType
from memory.index_sync import MemoryIndexSynchronizer
from tests.fakes.in_memory_memory_index import InMemoryMemoryIndex
from tests.fakes.in_memory_memory_repository import InMemoryMemoryRepository


NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)


def setup_memory():
    repository = InMemoryMemoryRepository()
    commands = MemoryCommandService(repository, clock=lambda: NOW)
    context = MemoryContext(user_id="alice", session_id="web:alice:a")
    proposal = MemoryWriteProposal(
        content="Use concise answers",
        kind=MemoryKind.PREFERENCE,
        owner_scope=MemoryOwnerScope.USER,
        owner_id="alice",
        source_type=MemorySourceType.EXPLICIT_USER,
        explicit_user_request=True,
    )
    return repository, commands, context, proposal


def test_outbox_upsert_and_delete_are_eventually_applied() -> None:
    repository, commands, context, proposal = setup_memory()
    index = InMemoryMemoryIndex()
    synchronizer = MemoryIndexSynchronizer(repository, index, clock=lambda: NOW)
    item = commands.remember(proposal, context)

    first = synchronizer.drain()
    commands.revoke(item.id, "forgotten", context)
    second = synchronizer.drain()

    assert first.completed == 1
    assert second.completed == 1
    assert index.items == {}


def test_index_failure_is_retried_without_rolling_back_fact() -> None:
    repository, commands, context, proposal = setup_memory()
    index = InMemoryMemoryIndex()
    index.fail_upsert = True
    item = commands.remember(proposal, context)
    synchronizer = MemoryIndexSynchronizer(repository, index, clock=lambda: NOW)

    result = synchronizer.drain()

    assert result.retried == 1
    assert repository.get(item.id) == item
    event = next(iter(repository.outbox.values()))
    assert event.status == "retry"
    assert event.attempt_count == 1


def test_stale_upsert_event_cannot_restore_revoked_memory() -> None:
    repository, commands, context, proposal = setup_memory()
    item = commands.remember(proposal, context)
    commands.revoke(item.id, "forgotten", context)
    index = InMemoryMemoryIndex()

    result = MemoryIndexSynchronizer(repository, index, clock=lambda: NOW).drain(limit=10)

    assert result.completed == 2
    assert index.items == {}
