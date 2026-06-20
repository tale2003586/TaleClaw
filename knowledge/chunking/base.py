from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class KnowledgeChunk:
    id: str
    text: str
    source_path: str
    source_relpath: str
    title: str
    chunk_index: int
    char_start: int
    char_end: int
    source_type: str
    metadata: dict


class ChunkingStrategy(Protocol):
    @property
    def source_type(self) -> str:
        ...

    def supports(self, path: Path) -> bool:
        ...

    def chunk(
        self,
        path: Path,
        *,
        root: Path,
        chunk_chars: int = 1800,
        overlap_chars: int = 220,
    ) -> list[KnowledgeChunk]:
        ...
