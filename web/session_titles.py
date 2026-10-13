from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

DEFAULT_SESSION_TITLE_MAX_CHARS = 20
_TITLE_EDGE_QUOTES = "\"'`“”‘’《》「」『』"


def first_question_answer(messages: list[dict[str, Any]]) -> tuple[str, str] | None:
    question = ""
    for message in messages or []:
        role = str(message.get("role", "") or "")
        text = _message_text(message).strip()
        if not question:
            if role == "user" and text:
                question = text
            continue
        if role == "assistant" and text:
            return question, text
    return None


def normalize_session_title(
    text: str,
    *,
    max_chars: int = DEFAULT_SESSION_TITLE_MAX_CHARS,
) -> str:
    title = re.sub(r"\s+", " ", str(text or "")).strip()
    title = title.strip(_TITLE_EDGE_QUOTES).strip()
    return title[: max(1, int(max_chars))].strip()


def session_display_title(
    session_or_row: Mapping[str, Any] | Any,
    fallback_id: str,
) -> str:
    if isinstance(session_or_row, Mapping):
        direct_title = session_or_row.get("title")
        metadata = session_or_row.get("metadata") or {}
    else:
        direct_title = getattr(session_or_row, "title", None)
        metadata = getattr(session_or_row, "metadata", {}) or {}
    title = str(direct_title or "").strip()
    if not title and isinstance(metadata, Mapping):
        title = str(metadata.get("title") or "").strip()
    return title or str(fallback_id)


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, Mapping):
                parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(getattr(block, "text", "") or ""))
        return "".join(parts)
    return "" if content is None else str(content)


__all__ = (
    "DEFAULT_SESSION_TITLE_MAX_CHARS",
    "first_question_answer",
    "normalize_session_title",
    "session_display_title",
)
