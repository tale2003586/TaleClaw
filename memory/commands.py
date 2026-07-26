from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from memory.domain import (
    MemoryEvidence,
    MemoryKind,
    MemoryOwnerScope,
    MemorySourceType,
    MemoryStatus,
    OwnerKey,
)


@dataclass(frozen=True)
class MemoryContext:
    user_id: str
    session_id: str
    application: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None

    def __post_init__(self) -> None:
        if not str(self.user_id or "").strip():
            raise ValueError("MemoryContext.user_id is required.")
        if not str(self.session_id or "").strip():
            raise ValueError("MemoryContext.session_id is required.")

    def allowed_owners(self) -> tuple[OwnerKey, ...]:
        owners = [OwnerKey(MemoryOwnerScope.USER, self.user_id)]
        for scope, value in (
            (MemoryOwnerScope.APPLICATION, self.application),
            (MemoryOwnerScope.WORKSPACE, self.workspace_id),
            (MemoryOwnerScope.PROJECT, self.project_id),
            (MemoryOwnerScope.TASK, self.task_id),
        ):
            if value:
                owners.append(OwnerKey(scope, value))
        return tuple(owners)

    def permits(self, owner: OwnerKey) -> bool:
        return owner in self.allowed_owners()

    @classmethod
    def from_session(cls, session) -> "MemoryContext":
        from user_scope import user_id_for_session

        metadata = getattr(session, "metadata", {}) or {}
        application = metadata.get("application")
        if not application and metadata.get("kind") == "coding_application":
            application = "coding"
        return cls(
            user_id=user_id_for_session(session),
            session_id=str(getattr(session, "id", "") or ""),
            application=str(application) if application else None,
            workspace_id=_optional_text(
                metadata.get("workspace_id") or metadata.get("workspace_root")
            ),
            project_id=_optional_text(
                metadata.get("project_id") or metadata.get("repository")
            ),
            task_id=_optional_text(metadata.get("task_id")),
        )


@dataclass(frozen=True)
class MemoryWriteProposal:
    content: str
    kind: MemoryKind
    owner_scope: MemoryOwnerScope
    owner_id: str
    source_type: MemorySourceType
    evidence: tuple[MemoryEvidence, ...] = ()
    confidence: float = 0.5
    salience: float = 0.5
    explicit_user_request: bool = False
    valid_until: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        content = str(self.content or "").strip()
        if not content:
            raise ValueError("Memory proposal content is required.")
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "kind", MemoryKind(self.kind))
        owner = OwnerKey(self.owner_scope, self.owner_id)
        object.__setattr__(self, "owner_scope", owner.scope)
        object.__setattr__(self, "owner_id", owner.id)
        object.__setattr__(self, "source_type", MemorySourceType(self.source_type))
        object.__setattr__(self, "evidence", tuple(self.evidence or ()))
        for name in ("confidence", "salience"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1.")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @property
    def owner(self) -> OwnerKey:
        return OwnerKey(self.owner_scope, self.owner_id)


@dataclass(frozen=True)
class MemoryTransition:
    memory_id: str
    target_status: MemoryStatus
    expected_version: int
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.memory_id or "").strip():
            raise ValueError("memory_id is required.")
        if int(self.expected_version) < 1:
            raise ValueError("expected_version must be positive.")
        object.__setattr__(self, "target_status", MemoryStatus(self.target_status))
        object.__setattr__(self, "expected_version", int(self.expected_version))
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        object.__setattr__(self, "metadata", dict(self.metadata or {}))


def _optional_text(value) -> str | None:
    text = str(value or "").strip()
    return text or None
