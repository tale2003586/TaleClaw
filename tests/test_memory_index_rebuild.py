from datetime import datetime, timezone

from memory.command_service import MemoryCommandService
from memory.commands import MemoryContext, MemoryWriteProposal
from memory.domain import MemoryKind, MemoryOwnerScope, MemorySourceType, OwnerKey
from memory.migration.rebuild_index import RebuildSemanticMemoryIndex
from tests.fakes.in_memory_memory_index import InMemoryMemoryIndex
from tests.fakes.in_memory_memory_repository import InMemoryMemoryRepository


def add(repository, owner_id, content):
    return MemoryCommandService(repository).remember(MemoryWriteProposal(
        content=content,
        kind=MemoryKind.FACT,
        owner_scope=MemoryOwnerScope.USER,
        owner_id=owner_id,
        source_type=MemorySourceType.EXPLICIT_USER,
        explicit_user_request=True,
    ), MemoryContext(user_id=owner_id, session_id=f"web:{owner_id}:a"))


def test_rebuild_dry_run_does_not_write_index() -> None:
    repository = InMemoryMemoryRepository()
    add(repository, "alice", "Alice fact")
    index = InMemoryMemoryIndex()

    report = RebuildSemanticMemoryIndex(repository, index).rebuild(dry_run=True)

    assert report.selected == 1
    assert report.indexed == 0
    assert index.items == {}


def test_rebuild_can_limit_owner_and_report_failures() -> None:
    repository = InMemoryMemoryRepository()
    alice = add(repository, "alice", "Alice fact")
    add(repository, "bob", "Bob fact")
    index = InMemoryMemoryIndex()

    report = RebuildSemanticMemoryIndex(repository, index).rebuild(
        owners=[OwnerKey(MemoryOwnerScope.USER, "alice")],
        dry_run=False,
    )

    assert report.selected == 1
    assert report.indexed == 1
    assert set(index.items) == {(alice.id, alice.version)}

    index.fail_upsert = True
    failed = RebuildSemanticMemoryIndex(repository, index).rebuild(
        owners=[OwnerKey(MemoryOwnerScope.USER, "alice")],
        dry_run=False,
    )
    assert failed.failed == 1
    assert alice.id in failed.errors[0]
