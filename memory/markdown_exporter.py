from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from memory.domain import OwnerKey


class MarkdownMemoryExporter:
    def __init__(self, repository) -> None:
        self.repository = repository

    def export(self, target: str | Path, *, owners: list[OwnerKey]) -> Path:
        now = datetime.now(timezone.utc)
        items = self.repository.list_active(owners, now)
        lines = [
            "# Generated Long-term Memory",
            "",
            "> GENERATED / READ-ONLY. PostgreSQL is the source of truth.",
            "",
        ]
        current_group = None
        for item in sorted(items, key=lambda value: (
            value.owner_scope.value,
            value.owner_id,
            value.kind.value,
            value.content,
        )):
            group = f"{item.owner_scope.value}:{item.owner_id} / {item.kind.value}"
            if group != current_group:
                lines.extend([f"## {group}", ""])
                current_group = group
            lines.append(f"- {item.content} <!-- memory_id={item.id} version={item.version} -->")
        output = "\n".join(lines).rstrip() + "\n"
        destination = Path(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            dir=destination.parent,
            text=True,
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(output)
            os.replace(temporary, destination)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return destination
