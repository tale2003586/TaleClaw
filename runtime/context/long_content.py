"""Detection and externalization of content unsafe for active prompt context."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping

from runtime.context.artifacts import ArtifactRef, ArtifactStore
from runtime.token_estimator import estimate_tokens


@dataclass(frozen=True)
class LongContentAssessment:
    is_long: bool
    token_count: int
    char_count: int
    byte_count: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ExternalizedContent:
    """Prompt-safe replacement and the reference to the immutable source."""

    content: str
    artifact_ref: ArtifactRef | None
    assessment: LongContentAssessment

    @property
    def externalized(self) -> bool:
        return self.artifact_ref is not None


class LongContentDetector:
    """Uses tokens as its decision authority, with hard size safety guards."""

    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        max_tokens: int = 4_000,
        max_chars: int = 20_000,
        max_bytes: int = 64_000,
        token_estimator: Callable[[str], int] | None = None,
        instruction_chars: int = 320,
    ) -> None:
        if min(max_tokens, max_chars, max_bytes) < 1:
            raise ValueError("long-content limits must be positive")
        self.artifact_store = artifact_store
        self.max_tokens = int(max_tokens)
        self.max_chars = int(max_chars)
        self.max_bytes = int(max_bytes)
        self.token_estimator = token_estimator or _estimate_text_tokens
        self.instruction_chars = max(40, int(instruction_chars))

    def assess(self, content: str | bytes | Mapping[str, Any] | list[Any]) -> LongContentAssessment:
        text = _to_text(content)
        byte_count = len(text.encode("utf-8"))
        token_count = int(self.token_estimator(text))
        reasons = []
        if token_count > self.max_tokens:
            reasons.append("token_limit")
        if len(text) > self.max_chars:
            reasons.append("character_guard")
        if byte_count > self.max_bytes:
            reasons.append("byte_guard")
        return LongContentAssessment(
            is_long=bool(reasons),
            token_count=max(0, token_count),
            char_count=len(text),
            byte_count=byte_count,
            reasons=tuple(reasons),
        )

    detect = assess

    def is_long_content(self, content: str | bytes | Mapping[str, Any] | list[Any]) -> bool:
        return self.assess(content).is_long

    def externalize(
        self,
        content: str | bytes | Mapping[str, Any] | list[Any],
        *,
        artifact_type: str = "text",
        name: str | None = None,
        mime_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExternalizedContent:
        text = _to_text(content)
        assessment = self.assess(text)
        if not assessment.is_long:
            return ExternalizedContent(text, None, assessment)
        ref = self.artifact_store.put_artifact(
            text,
            artifact_type=artifact_type,
            name=name,
            mime_type=mime_type,
            metadata=metadata,
        )
        return ExternalizedContent(
            content=self._replacement_instruction(text, ref, assessment),
            artifact_ref=ref,
            assessment=assessment,
        )

    def externalize_tool_result(
        self,
        output: str | bytes | Mapping[str, Any] | list[Any],
        *,
        name: str = "tool-result",
        metadata: Mapping[str, Any] | None = None,
    ) -> ExternalizedContent:
        return self.externalize(
            output,
            artifact_type="tool_result",
            name=name,
            metadata=metadata,
        )

    process = externalize

    def _replacement_instruction(
        self,
        original: str,
        ref: ArtifactRef,
        assessment: LongContentAssessment,
    ) -> str:
        instruction = _extract_instruction(original, self.instruction_chars)
        descriptor = (
            f"Large content was externalized to {ref.storage_uri} "
            f"({ref.name}; {assessment.token_count} tokens, {assessment.byte_count} bytes). "
            "Use read_artifact with this URI to inspect the complete content or search it."
        )
        return f"{instruction}\n\n{descriptor}\nartifact_ref: {ref.artifact_id}"


def _estimate_text_tokens(text: str) -> int:
    return estimate_tokens([{"role": "user", "content": text}])


def _to_text(content: str | bytes | Mapping[str, Any] | list[Any]) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    if isinstance(content, (Mapping, list)):
        return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
    raise TypeError("long-content input must be text, bytes, mapping, or list")


def _extract_instruction(text: str, limit: int) -> str:
    """Keep a human instruction only when the prefix looks like prose, not data."""
    stripped = text.lstrip()
    if not stripped or stripped[:1] in "{[":
        return "The supplied structured content is available by artifact reference."
    first_paragraph = stripped.split("\n\n", 1)[0].strip()
    first_line = first_paragraph.splitlines()[0].strip() if first_paragraph else ""
    candidate = first_paragraph if len(first_paragraph) <= limit else first_line
    if not candidate or len(candidate) > limit or _looks_like_log_line(candidate):
        return "The supplied large content is available by artifact reference."
    return candidate[:limit].rstrip()


def _looks_like_log_line(value: str) -> bool:
    lower = value.lower()
    return lower.startswith(("traceback", "exception", "error:", "info ", "debug "))
