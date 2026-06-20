from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable

from .base import KnowledgeChunk


CHUNKING_VERSION = 2


def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def normalize_text(text: str) -> str:
    return re.sub(r"\n{4,}", "\n\n\n", str(text or "").replace("\r\n", "\n").replace("\r", "\n")).strip()


def infer_title(path: Path, text: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    if match:
        return match.group(1).strip()
    return path.stem.replace("_", " ").replace("-", " ").strip() or path.name


def render_chunk_text(*, title: str, relpath: str, body: str) -> str:
    return f"TITLE: {title}\nSOURCE: {relpath}\n\n{body.strip()}"


def chunk_stable_id(path: Path, start: int, end: int, text: str) -> str:
    digest = hashlib.sha1(f"v{CHUNKING_VERSION}:{path}:{start}:{end}:{text[:120]}".encode("utf-8")).hexdigest()
    return f"security-kb:{digest}"


def make_chunk(
    *,
    path: Path,
    root: Path,
    title: str,
    body: str,
    chunk_index: int,
    char_start: int,
    char_end: int,
    source_type: str,
    metadata: dict,
) -> KnowledgeChunk:
    relpath = str(path.relative_to(root)) if path.is_relative_to(root) else path.name
    return KnowledgeChunk(
        id=chunk_stable_id(path, char_start, char_end, body),
        text=render_chunk_text(title=title, relpath=relpath, body=body),
        source_path=str(path),
        source_relpath=relpath,
        title=title,
        chunk_index=chunk_index,
        char_start=char_start,
        char_end=char_end,
        source_type=source_type,
        metadata=metadata,
    )


def split_markdown_sections(text: str) -> list[tuple[str, str, int]]:
    matches = list(re.finditer(r"(?m)^(#{1,6})\s+(.+?)\s*$", text))
    if not matches:
        return [("", text, 0)]
    sections: list[tuple[str, str, int]] = []
    if matches[0].start() > 0:
        sections.append(("", text[: matches[0].start()], 0))
    heading_stack: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        level = len(match.group(1))
        heading = match.group(2).strip()
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_stack.append((level, heading))
        sections.append((" > ".join(item[1] for item in heading_stack), text[match.start() : end], match.start()))
    return [(title, body, start) for title, body, start in sections if body.strip()]


def split_semantic_text(text: str, *, chunk_chars: int, overlap_chars: int) -> list[tuple[str, int, int]]:
    text = normalize_text(text)
    if len(text) <= chunk_chars:
        return [(text, 0, len(text))]
    chunks: list[tuple[str, int, int]] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_chars)
        if end < len(text):
            split_at = _semantic_split_point(text, start, end)
            if split_at > start + chunk_chars // 2:
                end = split_at + 1
            end = _avoid_open_code_fence(text, start, end) if end < len(text) else end
        if end <= start:
            end = min(len(text), start + max(1, chunk_chars))
        chunk = text[start:end].strip()
        if chunk:
            chunks.append((chunk, start, end))
        if end >= len(text):
            break
        next_start = _semantic_overlap_start(text, end, overlap_chars)
        if next_start <= start:
            next_start = end
        start = next_start
    return chunks


def dedupe_strings(items: Iterable[str]) -> list[str]:
    seen = set()
    output = []
    for item in items:
        normalized = str(item).strip()
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        output.append(normalized)
    return output


def as_list(value):
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _semantic_split_point(text: str, start: int, end: int) -> int:
    return max(
        text.rfind("\n\n", start, end),
        text.rfind("\n- ", start, end),
        text.rfind("\n* ", start, end),
        text.rfind("\n|", start, end),
        text.rfind("\n", start, end),
        text.rfind(". ", start, end),
        text.rfind("。", start, end),
    )


def _semantic_overlap_start(text: str, end: int, overlap_chars: int) -> int:
    if overlap_chars <= 0:
        return end
    raw_start = max(0, end - overlap_chars)
    paragraph_start = text.find("\n\n", raw_start, end)
    if paragraph_start != -1 and paragraph_start + 2 < end:
        return paragraph_start + 2
    line_start = text.find("\n", raw_start, end)
    if line_start != -1 and line_start + 1 < end:
        return line_start + 1
    return raw_start


def _avoid_open_code_fence(text: str, start: int, end: int) -> int:
    window = text[start:end]
    if window.count("```") % 2 == 0:
        return end
    fence_start = window.rfind("```")
    if fence_start > len(window) // 2:
        return start + fence_start
    fence_end = text.find("```", end)
    if fence_end != -1 and fence_end + 3 - start <= int((end - start) * 1.25):
        return fence_end + 3
    return end
