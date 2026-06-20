from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class IncrementalIngestResult:
    files_seen: int
    files_indexed: int
    files_deleted: int
    chunks_indexed: int

    def to_dict(self) -> dict[str, int]:
        return {
            "files_seen": self.files_seen,
            "files_indexed": self.files_indexed,
            "files_deleted": self.files_deleted,
            "chunks_indexed": self.chunks_indexed,
        }


class IncrementalIngester:
    """Synchronize changed source files into a SecurityKnowledgeIndex."""

    def __init__(
        self,
        *,
        index,
        source_root: Path,
        state_path: Path,
        iter_files: Callable[..., Iterable[Path]],
        chunk_file: Callable[..., list],
        max_file_bytes: int = 1_000_000,
        chunk_chars: int = 1800,
        overlap_chars: int = 220,
        batch_size: int = 64,
        limit_files: int | None = None,
    ) -> None:
        self.index = index
        self.source_root = source_root
        self.state_path = state_path.expanduser()
        self.iter_files = iter_files
        self.chunk_file = chunk_file
        self.max_file_bytes = int(max_file_bytes)
        self.chunk_chars = int(chunk_chars)
        self.overlap_chars = int(overlap_chars)
        self.batch_size = int(batch_size)
        self.limit_files = limit_files

    def sync(self) -> IncrementalIngestResult:
        state = self._load_state()
        current_files = list(self.iter_files(
            self.source_root,
            max_file_bytes=self.max_file_bytes,
            limit=self.limit_files,
        ))
        current_keys = {self._key(path) for path in current_files}
        deleted = sorted(set(state.get("files", {})) - current_keys)
        for key in deleted:
            source_path = state["files"].get(key, {}).get("source_path") or key
            self.index.delete_file_chunks(source_path)
            state["files"].pop(key, None)

        files_indexed = 0
        chunks_indexed = 0
        for path in current_files:
            key = self._key(path)
            fingerprint = self._fingerprint(path)
            if state.get("files", {}).get(key, {}).get("fingerprint") == fingerprint:
                continue
            self.index.delete_file_chunks(str(path))
            chunks = self.chunk_file(
                path,
                root=self.source_root,
                chunk_chars=self.chunk_chars,
                overlap_chars=self.overlap_chars,
            )
            chunks_indexed += self.index.upsert_chunks(
                chunks,
                batch_size=self.batch_size,
            )
            files_indexed += 1
            state.setdefault("files", {})[key] = {
                "fingerprint": fingerprint,
                "source_path": str(path),
                "chunk_count": len(chunks),
            }

        self._save_state(state)
        return IncrementalIngestResult(
            files_seen=len(current_files),
            files_indexed=files_indexed,
            files_deleted=len(deleted),
            chunks_indexed=chunks_indexed,
        )

    def _key(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.source_root))
        except ValueError:
            return str(path)

    def _fingerprint(self, path: Path) -> dict[str, Any]:
        stat = path.stat()
        return {
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"files": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"files": {}}
        if not isinstance(data, dict):
            return {"files": {}}
        files = data.get("files")
        if not isinstance(files, dict):
            data["files"] = {}
        return data

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
