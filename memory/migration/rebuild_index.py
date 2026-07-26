from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from memory.domain import OwnerKey


@dataclass(frozen=True)
class RebuildReport:
    dry_run: bool
    selected: int
    indexed: int
    failed: int
    digests: tuple[str, ...]
    errors: tuple[str, ...] = ()


class RebuildSemanticMemoryIndex:
    def __init__(self, repository, index) -> None:
        self.repository = repository
        self.index = index

    def rebuild(
        self,
        *,
        owners: list[OwnerKey] | None = None,
        dry_run: bool = True,
    ) -> RebuildReport:
        now = datetime.now(timezone.utc)
        items = (
            self.repository.list_active(owners, now)
            if owners
            else self.repository.list_all_active(now)
        )
        digests = tuple(
            hashlib.sha256(
                f"{item.id}\0{item.version}\0{item.content}".encode()
            ).hexdigest()
            for item in items
        )
        if dry_run:
            return RebuildReport(True, len(items), 0, 0, digests)
        indexed = 0
        errors = []
        for item in items:
            try:
                self.index.upsert(item)
                indexed += 1
            except Exception as exc:
                errors.append(f"{item.id}: {type(exc).__name__}: {exc}")
        return RebuildReport(
            False,
            len(items),
            indexed,
            len(errors),
            digests,
            tuple(errors),
        )
