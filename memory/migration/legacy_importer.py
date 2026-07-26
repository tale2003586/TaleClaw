from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from memory.commands import MemoryContext, MemoryWriteProposal
from memory.dedup import normalize_memory_text, parse_memory_items
from memory.domain import (
    MemoryEvidence,
    MemoryKind,
    MemoryOwnerScope,
    MemorySourceType,
)


@dataclass(frozen=True)
class MigrationItem:
    source: str
    action: str
    reason: str
    idempotency_key: str = ""
    memory_id: str = ""


@dataclass
class MigrationReport:
    dry_run: bool
    source_root: str
    user_id: str
    items: list[MigrationItem] = field(default_factory=list)

    def add(self, source: str, action: str, reason: str, **kwargs) -> None:
        self.items.append(MigrationItem(source, action, reason, **kwargs))

    def counts(self) -> dict[str, int]:
        values = {
            "imported": 0,
            "candidate": 0,
            "skipped": 0,
            "review": 0,
            "duplicate": 0,
            "conflict": 0,
            "failed": 0,
        }
        for item in self.items:
            values[item.action] = values.get(item.action, 0) + 1
        return values

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "source_root": self.source_root,
            "user_id": self.user_id,
            "counts": self.counts(),
            "items": [asdict(item) for item in self.items],
        }


class LegacyMemoryImporter:
    def __init__(self, command_service, repository) -> None:
        self.command_service = command_service
        self.repository = repository

    def import_root(
        self,
        source_root: str | Path,
        *,
        user_id: str,
        dry_run: bool = True,
        include_candidates: bool = True,
        checkpoint_path: str | Path | None = None,
    ) -> MigrationReport:
        root = Path(source_root).resolve()
        report = MigrationReport(bool(dry_run), str(root), user_id)
        context = MemoryContext(
            user_id=user_id,
            session_id=f"migration:{user_id}",
        )
        checkpoint = self._load_checkpoint(checkpoint_path)
        self._import_memory(root / "MEMORY.md", context, report, checkpoint, dry_run)
        if include_candidates:
            self._import_pending_json(
                root / "PENDING.json",
                context,
                report,
                checkpoint,
                dry_run,
            )
        else:
            report.add("PENDING.json", "skipped", "candidate import disabled")
        self._classify_nonsemantic(root, report)
        self._classify_pending_markdown(root, report)
        if checkpoint_path and not dry_run:
            self._write_checkpoint(checkpoint_path, checkpoint)
        return report

    def _import_memory(self, path, context, report, checkpoint, dry_run) -> None:
        if not path.exists():
            report.add(path.name, "skipped", "source file missing")
            return
        for index, raw in enumerate(parse_memory_items(path.read_text(encoding="utf-8")), start=1):
            content = raw.lstrip("-* ").strip()
            self._apply_item(
                source=f"{path.name}:{index}",
                content=content,
                context=context,
                report=report,
                checkpoint=checkpoint,
                dry_run=dry_run,
                active=True,
                confidence=0.9,
                metadata={"legacy_file": path.name, "legacy_line": index},
            )

    def _import_pending_json(self, path, context, report, checkpoint, dry_run) -> None:
        if not path.exists():
            report.add(path.name, "skipped", "source file missing")
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report.add(path.name, "failed", f"invalid JSON: {type(exc).__name__}")
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("candidates", []), list):
            report.add(path.name, "failed", "expected object with candidates array")
            return
        for index, value in enumerate(payload.get("candidates", []), start=1):
            if not isinstance(value, dict) or not str(value.get("content") or "").strip():
                report.add(f"{path.name}:{index}", "failed", "invalid candidate")
                continue
            self._apply_item(
                source=f"{path.name}:{index}",
                content=str(value["content"]),
                context=context,
                report=report,
                checkpoint=checkpoint,
                dry_run=dry_run,
                active=False,
                confidence=float(value.get("confidence") or 0.5),
                metadata={
                    "legacy_file": path.name,
                    "legacy_candidate_id": value.get("id"),
                    "evidence_count": value.get("evidence_count", 1),
                    "source_refs": value.get("source_refs", []),
                },
            )

    def _apply_item(
        self,
        *,
        source,
        content,
        context,
        report,
        checkpoint,
        dry_run,
        active,
        confidence,
        metadata,
    ) -> None:
        normalized = normalize_memory_text(content)
        if not normalized:
            report.add(source, "skipped", "empty normalized content")
            return
        key = _idempotency_key(source, context.user_id, normalized)
        existing = self.repository.find_exact(
            context.allowed_owners()[0],
            MemoryKind.FACT,
            normalized,
        )
        if key in checkpoint or existing is not None:
            report.add(
                source,
                "duplicate",
                "already imported",
                idempotency_key=key,
                memory_id=existing.id if existing else "",
            )
            return
        if dry_run:
            report.add(
                source,
                "imported" if active else "candidate",
                "dry-run; no write",
                idempotency_key=key,
            )
            return
        evidence = MemoryEvidence(
            id=f"legacy:{key[:24]}",
            memory_id="pending",
            source_type=MemorySourceType.LEGACY_IMPORT,
            source_ref=source,
            excerpt=content,
            metadata={"idempotency_key": key, **metadata},
        )
        proposal = MemoryWriteProposal(
            content=content,
            kind=MemoryKind.FACT,
            owner_scope=MemoryOwnerScope.USER,
            owner_id=context.user_id,
            source_type=MemorySourceType.LEGACY_IMPORT,
            evidence=(evidence,),
            confidence=max(0.0, min(1.0, confidence)),
            salience=0.6,
            explicit_user_request=active,
            metadata={"legacy_import_key": key, **metadata},
        )
        item = (
            self.command_service.remember(proposal, context)
            if active
            else self.command_service.propose(proposal, context)
        )
        checkpoint.add(key)
        report.add(
            source,
            "imported" if active else "candidate",
            "written",
            idempotency_key=key,
            memory_id=item.id,
        )

    def _classify_nonsemantic(self, root: Path, report: MigrationReport) -> None:
        for name in ("SELF.md", "NOW.md"):
            path = root / name
            report.add(name, "review" if path.exists() else "skipped", (
                "manual review; not a user semantic fact" if path.exists() else "source file missing"
            ))
        for name in ("HISTORY.md", "RECENT_CONTEXT.md", "RECENT_CONTEXT.json"):
            report.add(name, "skipped", "episodic legacy; never imported as active")

    def _classify_pending_markdown(self, root: Path, report: MigrationReport) -> None:
        path = root / "PENDING.md"
        if not path.exists():
            report.add(path.name, "skipped", "source file missing")
            return
        count = len(parse_memory_items(path.read_text(encoding="utf-8")))
        report.add(
            path.name,
            "review" if count else "skipped",
            f"{count} markdown candidate(s); compare with PENDING.json",
        )

    def _load_checkpoint(self, path) -> set[str]:
        if not path or not Path(path).exists():
            return set()
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        return {str(value) for value in payload.get("completed", [])} if isinstance(payload, dict) else set()

    def _write_checkpoint(self, path, checkpoint) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"completed": sorted(checkpoint)}, indent=2) + "\n",
            encoding="utf-8",
        )


def _idempotency_key(source: str, user_id: str, normalized: str) -> str:
    return hashlib.sha256(
        f"legacy_import\0{source}\0user\0{user_id}\0fact\0{normalized}".encode()
    ).hexdigest()
