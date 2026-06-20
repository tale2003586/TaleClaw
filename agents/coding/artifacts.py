from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .conclusions import ConclusionExtraction
from .promotion import PromotionResult
from .session import TaskSessionRecord


@dataclass(frozen=True)
class TaskArtifactPaths:
    task_log_path: Path
    conclusions_path: Path


class TaskArtifactWriter:
    """Persist detailed task audit logs separately from global memory."""

    def write(
        self,
        *,
        record: TaskSessionRecord,
        user_request: str,
        task_reply: str,
        extraction: ConclusionExtraction,
        promotion: PromotionResult,
    ) -> TaskArtifactPaths:
        task_root = record.memory_root.parent
        task_root.mkdir(parents=True, exist_ok=True)
        conclusions_path = task_root / "CONCLUSIONS.json"
        task_log_path = task_root / "TASK_LOG.md"

        conclusions_payload = {
            "task_id": record.task_id,
            "parent_session_id": record.parent_session_id,
            "summary": extraction.summary,
            "extraction_error": extraction.error,
            "raw_response": extraction.raw_response,
            "llm_candidates": [asdict(item) for item in extraction.candidates],
            "promoted": [asdict(item) for item in promotion.promoted],
            "skipped": [asdict(item) for item in promotion.skipped],
            "rejected": [asdict(item) for item in promotion.rejected],
        }
        conclusions_path.write_text(
            json.dumps(conclusions_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        task_log_path.write_text(
            self._format_log(
                record=record,
                user_request=user_request,
                task_reply=task_reply,
                extraction=extraction,
                promotion=promotion,
            ),
            encoding="utf-8",
        )
        return TaskArtifactPaths(
            task_log_path=task_log_path,
            conclusions_path=conclusions_path,
        )

    def _format_log(
        self,
        *,
        record: TaskSessionRecord,
        user_request: str,
        task_reply: str,
        extraction: ConclusionExtraction,
        promotion: PromotionResult,
    ) -> str:
        metadata = record.session.metadata or {}
        sections = [
            f"# Task Log: {record.task_id}",
            "",
            "## Metadata",
            "",
            f"- parent_session: `{record.parent_session_id}`",
            f"- task_type: `{record.task_type}`",
            f"- status: `{metadata.get('status', 'unknown')}`",
            f"- created_at: `{record.session.created_at}`",
            f"- updated_at: `{record.session.updated_at}`",
            "",
            "## User Request",
            "",
            _code_block(user_request),
            "",
            "## Final Reply",
            "",
            _code_block(task_reply),
            "",
            "## Conclusion Extraction",
            "",
            f"- summary: {extraction.summary or '(empty)'}",
            f"- error: {extraction.error or '(none)'}",
            f"- llm_candidates: {len(extraction.candidates)}",
            f"- promoted: {len(promotion.promoted)}",
            f"- skipped: {len(promotion.skipped)}",
            f"- rejected: {len(promotion.rejected)}",
            "",
            "## Transcript",
            "",
        ]
        for index, message in enumerate(record.session.messages):
            sections.extend(_format_message(index, message))
        return "\n".join(sections).rstrip() + "\n"


def _format_message(index: int, message: dict[str, Any]) -> list[str]:
    role = str(message.get("role", "message"))
    lines = [
        f"### {index}. {role}",
        "",
    ]
    metadata = {
        key: value
        for key, value in message.items()
        if key not in {"role", "content"}
    }
    if metadata:
        lines.extend([
            "Metadata:",
            "",
            _code_block(json.dumps(metadata, indent=2, ensure_ascii=False, default=str), "json"),
            "",
        ])
    lines.extend([
        "Content:",
        "",
        _code_block(_message_content(message)),
        "",
    ])
    return lines


def _message_content(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, indent=2, ensure_ascii=False, default=str)


def _code_block(content: str, language: str = "text") -> str:
    return f"~~~{language}\n{str(content or '').strip()}\n~~~"
