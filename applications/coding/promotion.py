from __future__ import annotations

from dataclasses import dataclass, field

from .conclusions import ConclusionCandidate


ALLOWED_CATEGORIES = {"project", "decision", "preference", "fact", "task"}
MIN_LLM_CONFIDENCE = 0.65
MAX_CANDIDATE_LENGTH = 360
MAX_CANDIDATE_LINES = 4
_NOISY_MARKERS = {
    "<task-session",
    "</task-session>",
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
    """Write filtered coding conclusions to semantic memory when enabled."""

    def __init__(
        self,
        command_service=None,
    ) -> None:
        self.command_service = command_service

    def promote(
        self,
        *,
        task_id: str,
        extracted_conclusions: list[ConclusionCandidate] | None = None,
        memory_context=None,
        repository_revision: str = "",
    ) -> PromotionResult:
        result = PromotionResult()
        if self.command_service is None:
            return result
        from memory.commands import MemoryWriteProposal
        from memory.domain import (
            MemoryEvidence,
            MemoryKind,
            MemoryOwnerScope,
            MemorySourceType,
        )

        candidates = _dedupe(extracted_conclusions or [])
        for candidate in candidates:
            reason = _rejection_reason(candidate)
            if reason:
                result.rejected.append(RejectedConclusion(candidate=candidate, reason=reason))
                continue
            if memory_context is None:
                result.rejected.append(RejectedConclusion(
                    candidate=candidate,
                    reason="trusted memory context is required",
                ))
                continue
            owner_scope, owner_id = _coding_owner(memory_context, MemoryOwnerScope)
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
            if not verified:
                result.rejected.append(RejectedConclusion(
                    candidate=candidate,
                    reason="coding conclusion is not verified",
                ))
                continue
            self.command_service.record_verified_conclusion(MemoryWriteProposal(
                content=candidate.content,
                kind=_kind_for_category(candidate.category, MemoryKind),
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
        return result


def _dedupe(items: list[ConclusionCandidate]) -> list[ConclusionCandidate]:
    from memory.dedup import normalize_memory_text

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
    from memory.dedup import normalize_memory_text

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


def _coding_owner(context, owner_scope_type) -> tuple[object, str]:
    if context.project_id:
        return owner_scope_type.PROJECT, context.project_id
    if context.workspace_id:
        return owner_scope_type.WORKSPACE, context.workspace_id
    if context.task_id:
        return owner_scope_type.TASK, context.task_id
    raise ValueError("Coding conclusion requires project, workspace, or task scope.")


def _kind_for_category(category: str, memory_kind):
    return {
        "decision": memory_kind.DECISION,
        "preference": memory_kind.PREFERENCE,
        "project": memory_kind.FACT,
        "fact": memory_kind.FACT,
        "task": memory_kind.FACT,
    }.get(str(category or "").lower(), memory_kind.FACT)


def _evidence_id(task_id: str, candidate: ConclusionCandidate) -> str:
    import hashlib

    digest = hashlib.sha256(
        f"{task_id}\0{candidate.content}\0{candidate.evidence}".encode()
    ).hexdigest()[:24]
    return f"coding:{digest}"
