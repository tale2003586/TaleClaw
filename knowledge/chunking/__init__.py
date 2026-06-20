from __future__ import annotations

from pathlib import Path

from .advisory import JsonAdvisoryChunking
from .base import ChunkingStrategy, KnowledgeChunk
from .markdown import MarkdownDocChunking
from .semgrep import SemgrepYamlChunking


class ChunkingRouter:
    def __init__(self, strategies: list[ChunkingStrategy] | None = None) -> None:
        self.strategies = strategies or [
            JsonAdvisoryChunking(),
            SemgrepYamlChunking(),
            MarkdownDocChunking(),
        ]
        self.default = MarkdownDocChunking()

    def strategy_for(self, path: Path) -> ChunkingStrategy:
        for strategy in self.strategies:
            if strategy.supports(path):
                return strategy
        return self.default

    def chunks_from_file(
        self,
        path: Path,
        *,
        root: Path,
        chunk_chars: int = 1800,
        overlap_chars: int = 220,
    ) -> list[KnowledgeChunk]:
        strategy = self.strategy_for(path)
        chunks = strategy.chunk(
            path,
            root=root,
            chunk_chars=chunk_chars,
            overlap_chars=overlap_chars,
        )
        if chunks:
            return chunks
        if strategy is not self.default:
            return self.default.chunk(
                path,
                root=root,
                chunk_chars=chunk_chars,
                overlap_chars=overlap_chars,
            )
        return []


__all__ = [
    "ChunkingRouter",
    "ChunkingStrategy",
    "JsonAdvisoryChunking",
    "KnowledgeChunk",
    "MarkdownDocChunking",
    "SemgrepYamlChunking",
]
