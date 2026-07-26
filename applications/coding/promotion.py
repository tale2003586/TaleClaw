from __future__ import annotations

import re
from dataclasses import dataclass, field

from memory.dedup import normalize_memory_text
from memory.store import MemoryStore
from memory.commands import MemoryContext, MemoryWriteProposal
from memory.domain import (
    MemoryEvidence,
    MemoryKind,
    MemoryOwnerScope,
    MemorySourceType,
)
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

    def __init__(
        self,
        global_memory: MemoryStore | None = None,
        *,
        command_service=None,
    ) -> None:
        self.global_memory = global_memory
        self.command_service = command_service

    def promote(
        self,
        *,
        task_id: str,
        task_memory: MemoryStore,
        extracted_conclusions: list[ConclusionCandidate] | None = None,
        memory_context: MemoryContext | None = None,
        repository_revision: str = "",
    ) -> PromotionResult:
        result = PromotionResult()
        candidates = self._collect_candidates(task_memory, extracted_conclusions or [])
        for candidate in candidates:
            reason = _rejection_reason(candidate)
            if reason:
                result.rejected.append(RejectedConclusion(candidate=candidate, reason=reason))
                continue
            if self.command_service is not None:
                if memory_context is None:
                    result.rejected.append(RejectedConclusion(
                        candidate=candidate,
                        reason="trusted memory context is required",
                    ))
                    continue
                owner_scope, owner_id = _coding_owner(memory_context)
                evidence_file = candidate.evidence_file or candidate.evidence
                evidence_location = candidate.evidence_location
                verified = bool(candidate.verified)
                evidence = MemoryEvidence(
                    id=_evidence_id(task_id, candidate),
                    memory_id="pending",
                    source_type=MemorySourceType.CODING_CONCLUSION,
                    source_ref=(
                        f"task:{task_id}/{evidence_file}"
                        + (f":{evidence_location}" if evidence_location else "")
                    ),
                    session_id=memory_context.session_id,
                    task_id=task_id,
                    workspace_id=memory_context.workspace_id,
                    project_id=memory_context.project_id,
                    excerpt=candidate.content,
                    metadata={
                        "category": candidate.category,
                        "evidence": candidate.evidence,
                        "evidence_file": evidence_file,
                        "evidence_location": evidence_location,
                        "code_revision": candidate.code_revision or repository_revision,
                        "verified": verified,
                    },
                )
                self.command_service.propose(MemoryWriteProposal(
                    content=candidate.content,
                    kind=_kind_for_category(candidate.category),
                    owner_scope=owner_scope,
                    owner_id=owner_id,
                    source_type=MemorySourceType.CODING_CONCLUSION,
                    evidence=(evidence,),
                    confidence=candidate.confidence,
                    salience=0.7,
                    metadata={
                        "entrypoint": "coding_conclusion",
                        "repository_revision": candidate.code_revision or repository_revision,
                        "verified": verified,
                    },
                ), memory_context)
                result.promoted.append(candidate)
                continue
            if self.global_memory is None:
                result.rejected.append(RejectedConclusion(
                    candidate=candidate,
                    reason="legacy global memory is unavailable",
                ))
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


def _coding_owner(context: MemoryContext) -> tuple[MemoryOwnerScope, str]:
    if context.project_id:
        return MemoryOwnerScope.PROJECT, context.project_id
    if context.workspace_id:
        return MemoryOwnerScope.WORKSPACE, context.workspace_id
    if context.task_id:
        return MemoryOwnerScope.TASK, context.task_id
    raise ValueError("Coding conclusion requires project, workspace, or task scope.")


def _kind_for_category(category: str) -> MemoryKind:
    return {
        "decision": MemoryKind.DECISION,
        "preference": MemoryKind.PREFERENCE,
        "project": MemoryKind.FACT,
        "fact": MemoryKind.FACT,
        "task": MemoryKind.FACT,
    }.get(str(category or "").lower(), MemoryKind.FACT)


def _evidence_id(task_id: str, candidate: ConclusionCandidate) -> str:
    import hashlib

    digest = hashlib.sha256(
        f"{task_id}\0{candidate.content}\0{candidate.evidence}".encode()
    ).hexdigest()[:24]
    return f"coding:{digest}"
