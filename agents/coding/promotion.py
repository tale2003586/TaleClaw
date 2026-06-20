from __future__ import annotations

import re
from dataclasses import dataclass, field

from memory.dedup import normalize_memory_text
from memory.store import MemoryStore
from .conclusions import ConclusionCandidate


ALLOWED_CATEGORIES = {"project", "decision", "preference", "fact", "task"}
MIN_LLM_CONFIDENCE = 0.65
MAX_CANDIDATE_LENGTH = 360
MAX_CANDIDATE_LINES = 4
_TAG_PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*")
_SOURCE_SUFFIX_RE = re.compile(r"\s*\(source:\s*`[^`]+`\)\s*$")
_NOISY_MARKERS = {
    "<task-session",
    "</task-session>",
    "<global-memory-snapshot",
    "</global-memory-snapshot>",
    "task recent context:",
    "task summary:",
    "latest_user:",
    "latest_assistant:",
}
_NOISY_PREFIXES = {
    "session:",
    "mode:",
    "source_ref:",
}


@dataclass(frozen=True)
class RejectedConclusion:
    candidate: ConclusionCandidate
    reason: str


@dataclass
class PromotionResult:
    promoted: list[ConclusionCandidate] = field(default_factory=list)
    skipped: list[ConclusionCandidate] = field(default_factory=list)
    rejected: list[RejectedConclusion] = field(default_factory=list)


class TaskMemoryPromoter:
    """Promote filtered task conclusions into global pending memory."""

    def __init__(self, global_memory: MemoryStore) -> None:
        self.global_memory = global_memory

    def promote(
        self,
        *,
        task_id: str,
        task_memory: MemoryStore,
        extracted_conclusions: list[ConclusionCandidate] | None = None,
    ) -> PromotionResult:
        result = PromotionResult()
        candidates = self._collect_candidates(task_memory, extracted_conclusions or [])
        for candidate in candidates:
            reason = _rejection_reason(candidate)
            if reason:
                result.rejected.append(RejectedConclusion(candidate=candidate, reason=reason))
                continue
            save_result = self.global_memory.append_pending(
                candidate.content,
                tag=candidate.category,
                source_ref=f"task:{task_id}/{candidate.source}",
            )
            if save_result.startswith("Saved"):
                result.promoted.append(candidate)
            else:
                result.skipped.append(candidate)
        return result

    def _collect_candidates(
        self,
        task_memory: MemoryStore,
        extracted_conclusions: list[ConclusionCandidate],
    ) -> list[ConclusionCandidate]:
        explicit = [
            ConclusionCandidate(
                category="task",
                content=item,
                confidence=1.0,
                source="explicit",
            )
            for item in _bullet_items(task_memory.read_pending())
        ]
        return _dedupe([*explicit, *extracted_conclusions])


def _bullet_items(markdown: str) -> list[str]:
    items: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("-", "*")):
            continue
        content = stripped[1:].strip()
        content = _TAG_PREFIX_RE.sub("", content)
        content = _SOURCE_SUFFIX_RE.sub("", content).strip()
        if content:
            items.append(content)
    return items


def _dedupe(items: list[ConclusionCandidate]) -> list[ConclusionCandidate]:
    seen = set()
    out = []
    for item in items:
        key = normalize_memory_text(item.content)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _rejection_reason(candidate: ConclusionCandidate) -> str:
    content = candidate.content.strip()
    lowered = content.lower()
    category = candidate.category.strip().lower()
    if category not in ALLOWED_CATEGORIES:
        return f"unsupported category: {category or '(empty)'}"
    if candidate.source == "llm" and candidate.confidence < MIN_LLM_CONFIDENCE:
        return f"confidence below {MIN_LLM_CONFIDENCE}"
    if not normalize_memory_text(content):
        return "empty content"
    if len(content) > MAX_CANDIDATE_LENGTH:
        return f"content exceeds {MAX_CANDIDATE_LENGTH} characters"
    if len(content.splitlines()) > MAX_CANDIDATE_LINES:
        return f"content exceeds {MAX_CANDIDATE_LINES} lines"
    if any(marker in lowered for marker in _NOISY_MARKERS):
        return "contains task wrapper or transcript marker"
    if any(lowered.startswith(prefix) for prefix in _NOISY_PREFIXES):
        return "contains task metadata marker"
    return ""
