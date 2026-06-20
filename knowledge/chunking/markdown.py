from __future__ import annotations

from pathlib import Path

from .base import KnowledgeChunk
from .utils import infer_title, make_chunk, read_text_file, split_markdown_sections, split_semantic_text


class MarkdownDocChunking:
    source_type = "markdown"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in {".md", ".markdown", ".rst", ".txt", ".py"}

    def chunk(
        self,
        path: Path,
        *,
        root: Path,
        chunk_chars: int = 1800,
        overlap_chars: int = 220,
    ) -> list[KnowledgeChunk]:
        text = read_text_file(path)
        if not text.strip():
            return []
        title = infer_title(path, text)
        sections = split_markdown_sections(text) if path.suffix.lower() in {".md", ".markdown", ".rst"} else [("", text, 0)]
        chunks: list[KnowledgeChunk] = []
        for heading_path, section_text, section_start in sections:
            base_title = heading_path or title
            for item_text, start, end in split_semantic_text(section_text, chunk_chars=chunk_chars, overlap_chars=overlap_chars):
                chunks.append(
                    make_chunk(
                        path=path,
                        root=root,
                        title=base_title,
                        body=item_text,
                        chunk_index=len(chunks),
                        char_start=section_start + start,
                        char_end=section_start + end,
                        source_type=self.source_type if path.suffix.lower() in {".md", ".markdown", ".rst"} else path.suffix.lower().lstrip(".") or "text",
                        metadata={
                            "corpus_type": "markdown_doc" if path.suffix.lower() in {".md", ".markdown", ".rst"} else "plain_text",
                            "filename": path.name,
                            "parent": path.parent.name,
                            "heading": heading_path,
                            "strategy_version": 2,
                        },
                    )
                )
        return chunks
