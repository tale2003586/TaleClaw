"""Read-only access to pre-semantic Markdown memory files.

Remove this adapter after all user memory directories have been imported into
MemoryRepository and the Web legacy-memory view has been retired.
"""

from __future__ import annotations

from pathlib import Path

from user_scope import user_data_root


def read_legacy_memory_files(
    workspace: str | Path,
    user_id: str,
    names: tuple[str, ...],
) -> list[dict[str, str]]:
    root = user_data_root(workspace, user_id) / "memory"
    return [
        {
            "name": name,
            "content": (root / name).read_text(encoding="utf-8")
            if (root / name).is_file()
            else "",
        }
        for name in names
    ]
