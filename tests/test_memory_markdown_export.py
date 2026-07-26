from datetime import datetime, timezone
from pathlib import Path

from memory.command_service import MemoryCommandService
from memory.commands import MemoryContext, MemoryWriteProposal
from memory.domain import MemoryKind, MemoryOwnerScope, MemorySourceType, OwnerKey
from memory.markdown_exporter import MarkdownMemoryExporter
from tests.fakes.in_memory_memory_repository import InMemoryMemoryRepository


def test_export_is_generated_read_only_view_of_active_memory(tmp_path: Path) -> None:
    repository = InMemoryMemoryRepository()
    commands = MemoryCommandService(repository)
    context = MemoryContext(user_id="alice", session_id="web:alice:a")
    active = commands.remember(MemoryWriteProposal(
        content="Prefer concise answers",
        kind=MemoryKind.PREFERENCE,
        owner_scope=MemoryOwnerScope.USER,
        owner_id="alice",
        source_type=MemorySourceType.EXPLICIT_USER,
        explicit_user_request=True,
    ), context)
    revoked = commands.remember(MemoryWriteProposal(
        content="Prefer tables",
        kind=MemoryKind.PREFERENCE,
        owner_scope=MemoryOwnerScope.USER,
        owner_id="alice",
        source_type=MemorySourceType.EXPLICIT_USER,
        explicit_user_request=True,
    ), context)
    commands.revoke(revoked.id, "forgotten", context)
    target = tmp_path / "MEMORY.md"

    MarkdownMemoryExporter(repository).export(
        target,
        owners=[OwnerKey(MemoryOwnerScope.USER, "alice")],
    )

    text = target.read_text(encoding="utf-8")
    assert "GENERATED / READ-ONLY" in text
    assert "PostgreSQL is the source of truth" in text
    assert "Prefer concise answers" in text
    assert active.id in text
    assert "Prefer tables" not in text
