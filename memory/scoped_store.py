from __future__ import annotations

from pathlib import Path

from memory.store import MemoryStore
from user_scope import memory_root_for_session, memory_root_for_user


class ScopedMemoryStore:
    """Resolve durable memory from the active session's user boundary."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        legacy_store: MemoryStore | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.legacy_store = legacy_store or MemoryStore(self.workspace / "memory")
        self._stores: dict[Path, MemoryStore] = {
            self.legacy_store.root.resolve(): self.legacy_store,
        }

    def for_session(self, session) -> MemoryStore:
        return self._store(memory_root_for_session(self.workspace, session))

    def for_user(self, user_id: str) -> MemoryStore:
        return self._store(memory_root_for_user(self.workspace, user_id))

    def _store(self, root: Path) -> MemoryStore:
        resolved = root.resolve()
        if resolved not in self._stores:
            self._stores[resolved] = MemoryStore(resolved)
        return self._stores[resolved]
