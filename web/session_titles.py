from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from models.model_task_runner import ModelTaskRunner
from runtime.agent_spec import AgentSpec
from runtime.sessions import Session


DEFAULT_SESSION_TITLE_MAX_CHARS = 20
DEFAULT_SESSION_TITLE_TIMEOUT_SECONDS = 8.0
_TITLE_EDGE_QUOTES = "\"'`“”‘’《》「」『』"


@dataclass(frozen=True)
class SessionTitleResult:
    title: str
    source: Literal["existing", "model", "fallback", "unavailable"]
    updated: bool


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


class WebSessionTitleService:
    def __init__(
        self,
        *,
        runner: ModelTaskRunner,
        timeout_seconds: float = DEFAULT_SESSION_TITLE_TIMEOUT_SECONDS,
        max_chars: int = DEFAULT_SESSION_TITLE_MAX_CHARS,
    ) -> None:
        self.runner = runner
        self.timeout_seconds = max(0.001, float(timeout_seconds))
        self.max_chars = max(1, int(max_chars))
        self.spec = AgentSpec(
            name="web_session_title",
            model_purpose="summary",
            max_tokens=48,
        )

    async def ensure_title(self, session: Session) -> SessionTitleResult:
        existing = str((session.metadata or {}).get("title") or "").strip()
        if existing:
            return SessionTitleResult(existing, "existing", False)

        first_pair = first_question_answer(session.messages)
        if first_pair is None:
            return SessionTitleResult("", "unavailable", False)
        question, answer = first_pair

        source: Literal["model", "fallback"] = "model"
        try:
            generated = await asyncio.wait_for(
                asyncio.to_thread(self._generate_title, question, answer),
                timeout=self.timeout_seconds,
            )
            title = normalize_session_title(generated, max_chars=self.max_chars)
        except Exception:
            title = ""

        if not title:
            source = "fallback"
            title = normalize_session_title(question, max_chars=self.max_chars)
        if not title:
            title = "新会话"[: self.max_chars]

        session.metadata["title"] = title
        return SessionTitleResult(title, source, True)

    def _generate_title(self, question: str, answer: str) -> str:
        return self.runner.run(
            spec=self.spec,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "根据用户的第一个问题和助手的回答，总结一个会话主题。"
                        "只输出标题，不要解释、引号、换行或 Markdown。"
                        "标题不得超过20个字符。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"问题：\n{question}\n\n回答：\n{answer}",
                },
            ],
            max_tokens=48,
        )


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
    "DEFAULT_SESSION_TITLE_TIMEOUT_SECONDS",
    "SessionTitleResult",
    "WebSessionTitleService",
    "first_question_answer",
    "normalize_session_title",
    "session_display_title",
)
