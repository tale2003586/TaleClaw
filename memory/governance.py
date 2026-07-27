from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol

from memory.domain import MemoryOwnerScope
from memory.notes import MemoryNoteOrigin


class MemoryPolicyAction(StrEnum):
    WRITE_STABLE = "write_stable"
    WRITE_PENDING = "write_pending"
    WRITE_TASK_LOCAL = "write_task_local"
    CREATE_LINK_CANDIDATE = "create_link_candidate"
    CREATE_EVOLUTION_PROPOSAL = "create_evolution_proposal"
    DISCARD = "discard"
    REQUIRE_USER_CONFIRMATION = "require_user_confirmation"


@dataclass(frozen=True)
class MemoryWriteRequest:
    content: str
    origin: MemoryNoteOrigin
    scope: MemoryOwnerScope
    scope_id: str
    confidence: float = 0.5
    source: str = ""
    requested_stable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.content or "").strip() or not str(self.scope_id or "").strip():
            raise ValueError("MemoryWriteRequest content and scope_id are required.")
        object.__setattr__(self, "origin", MemoryNoteOrigin(self.origin))
        object.__setattr__(self, "scope", MemoryOwnerScope(self.scope))
        value = float(self.confidence)
        if not 0 <= value <= 1:
            raise ValueError("confidence must be between 0 and 1.")
        object.__setattr__(self, "confidence", value)
        object.__setattr__(self, "metadata", dict(self.metadata or {}))


@dataclass(frozen=True)
class MemoryClassification:
    sensitive: bool = False
    prompt_injection: bool = False
    source_missing: bool = False
    task_local: bool = False
    inferred: bool = False
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryPolicyDecision:
    action: MemoryPolicyAction
    reason: str
    allow_stable_write: bool = False


@dataclass(frozen=True)
class MemoryAuditRecord:
    action: MemoryPolicyAction
    reason: str
    content_digest: str
    content_preview: str
    scope: str
    scope_id: str
    origin: str
    confidence: float
    classification: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class MemoryWriteResult:
    request: MemoryWriteRequest
    classification: MemoryClassification
    decision: MemoryPolicyDecision
    audit: MemoryAuditRecord


class SensitiveMemoryDetector(Protocol):
    def reasons(self, content: str) -> tuple[str, ...]: ...


class ConservativeSensitiveDetector:
    _patterns = (
        ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I)),
        ("api_key", re.compile(r"\b(?:api[_ -]?key|access[_ -]?token)\s*[:=]\s*\S{8,}", re.I)),
        ("password", re.compile(r"\bpassword\s*[:=]\s*\S{4,}", re.I)),
        ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}", re.I)),
    )

    def reasons(self, content: str) -> tuple[str, ...]:
        return tuple(name for name, pattern in self._patterns if pattern.search(content or ""))


class MemoryGovernancePipeline:
    def __init__(self, detector: SensitiveMemoryDetector | None = None) -> None:
        self.detector = detector or ConservativeSensitiveDetector()

    def evaluate(self, request: MemoryWriteRequest) -> MemoryWriteResult:
        secret_reasons = self.detector.reasons(request.content)
        injection = _looks_like_prompt_injection(request.content)
        source_missing = not str(request.source or "").strip()
        task_local = request.scope is MemoryOwnerScope.TASK
        inferred = request.origin is MemoryNoteOrigin.INFERRED_BY_LLM
        reasons = [*secret_reasons]
        if injection:
            reasons.append("prompt_injection_pattern")
        if source_missing:
            reasons.append("source_missing")
        classification = MemoryClassification(
            sensitive=bool(secret_reasons), prompt_injection=injection,
            source_missing=source_missing, task_local=task_local,
            inferred=inferred, reasons=tuple(reasons),
        )
        decision = self._decide(request, classification)
        digest = hashlib.sha256(request.content.encode()).hexdigest()[:16]
        preview = "[redacted]" if classification.sensitive else request.content[:120]
        audit = MemoryAuditRecord(
            action=decision.action, reason=decision.reason,
            content_digest=digest, content_preview=preview,
            scope=request.scope.value, scope_id=request.scope_id,
            origin=request.origin.value, confidence=request.confidence,
            classification={
                "sensitive": classification.sensitive,
                "prompt_injection": classification.prompt_injection,
                "source_missing": classification.source_missing,
                "task_local": classification.task_local,
                "inferred": classification.inferred,
                "reasons": list(classification.reasons),
            },
        )
        return MemoryWriteResult(request, classification, decision, audit)

    def _decide(self, request, classification) -> MemoryPolicyDecision:
        if classification.sensitive or classification.prompt_injection:
            return MemoryPolicyDecision(MemoryPolicyAction.DISCARD, "unsafe_candidate")
        if request.metadata.get("contradicts") or request.metadata.get("supersedes"):
            return MemoryPolicyDecision(
                MemoryPolicyAction.CREATE_EVOLUTION_PROPOSAL,
                "existing_memory_change_requires_review",
            )
        if classification.task_local:
            return MemoryPolicyDecision(MemoryPolicyAction.WRITE_TASK_LOCAL, "task_scope_preserved")
        if classification.source_missing:
            return MemoryPolicyDecision(MemoryPolicyAction.WRITE_PENDING, "source_required_for_stable")
        if request.origin in {
            MemoryNoteOrigin.INFERRED_BY_LLM,
            MemoryNoteOrigin.TOOL_RESULT,
            MemoryNoteOrigin.TASK_SUMMARY,
            MemoryNoteOrigin.SYSTEM_EVENT,
        }:
            return MemoryPolicyDecision(MemoryPolicyAction.WRITE_PENDING, "non_user_origin_requires_review")
        if request.confidence < 0.65:
            return MemoryPolicyDecision(MemoryPolicyAction.WRITE_PENDING, "low_confidence")
        if request.origin is MemoryNoteOrigin.EXPLICIT_USER and request.requested_stable:
            return MemoryPolicyDecision(
                MemoryPolicyAction.WRITE_STABLE, "explicit_user_request", True
            )
        return MemoryPolicyDecision(MemoryPolicyAction.WRITE_PENDING, "conservative_default")


def _looks_like_prompt_injection(content: str) -> bool:
    text = str(content or "").lower()
    markers = (
        "ignore previous instructions", "ignore all previous", "system prompt",
        "忽略之前的指令", "忽略所有指令", "覆盖系统提示", "你现在必须",
    )
    return any(marker in text for marker in markers)
