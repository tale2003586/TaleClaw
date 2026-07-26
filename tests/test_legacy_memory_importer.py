import hashlib
import json
from pathlib import Path

from memory.command_service import MemoryCommandService
from memory.domain import MemoryStatus
from memory.migration.legacy_importer import LegacyMemoryImporter
from tests.fakes.in_memory_memory_repository import InMemoryMemoryRepository


def write_legacy(root: Path) -> None:
    root.mkdir()
    (root / "MEMORY.md").write_text(
        "# Memory\n\n- Prefer concise answers\n- Project uses pytest\n",
        encoding="utf-8",
    )
    (root / "PENDING.json").write_text(json.dumps({
        "version": 1,
        "candidates": [{
            "id": "cand-1",
            "content": "Maybe prefer tables",
            "confidence": 0.6,
            "evidence_count": 1,
        }],
    }), encoding="utf-8")
    (root / "PENDING.md").write_text("# Pending\n\n- orphan pending\n", encoding="utf-8")
    (root / "SELF.md").write_text("agent state", encoding="utf-8")
    (root / "NOW.md").write_text("current state", encoding="utf-8")
    (root / "HISTORY.md").write_text("private history", encoding="utf-8")
    (root / "RECENT_CONTEXT.md").write_text("recent history", encoding="utf-8")
    (root / "RECENT_CONTEXT.json").write_text('{"turns": []}', encoding="utf-8")


def checksums(root: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.iterdir()
        if path.is_file()
    }


def test_dry_run_has_zero_writes_and_classifies_every_legacy_file(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    write_legacy(root)
    before = checksums(root)
    repository = InMemoryMemoryRepository()

    report = LegacyMemoryImporter(
        MemoryCommandService(repository),
        repository,
    ).import_root(root, user_id="alice", dry_run=True, include_candidates=True)

    assert repository.items == {}
    assert checksums(root) == before
    sources = {item.source.split(":", 1)[0] for item in report.items}
    assert {
        "MEMORY.md", "PENDING.json", "PENDING.md", "SELF.md", "NOW.md",
        "HISTORY.md", "RECENT_CONTEXT.md", "RECENT_CONTEXT.json",
    } <= sources
    assert report.counts()["imported"] == 2
    assert report.counts()["candidate"] == 1
    assert report.counts()["review"] == 3


def test_apply_is_idempotent_and_never_imports_history_as_active(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    write_legacy(root)
    before = checksums(root)
    repository = InMemoryMemoryRepository()
    importer = LegacyMemoryImporter(MemoryCommandService(repository), repository)

    first = importer.import_root(root, user_id="alice", dry_run=False, include_candidates=True)
    second = importer.import_root(root, user_id="alice", dry_run=False, include_candidates=True)

    assert first.counts()["imported"] == 2
    assert first.counts()["candidate"] == 1
    assert second.counts()["duplicate"] == 3
    assert len(repository.items) == 3
    assert sum(item.status is MemoryStatus.ACTIVE for item in repository.items.values()) == 2
    assert all("private history" not in item.content for item in repository.items.values())
    assert checksums(root) == before


def test_invalid_pending_json_is_reported_not_guessed(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    root.mkdir()
    (root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
    (root / "PENDING.json").write_text("[]", encoding="utf-8")
    repository = InMemoryMemoryRepository()

    report = LegacyMemoryImporter(
        MemoryCommandService(repository),
        repository,
    ).import_root(root, user_id="alice", include_candidates=True)

    assert report.counts()["failed"] == 1
    assert repository.items == {}
