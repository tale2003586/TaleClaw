from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryOwnerScope(StrEnum):
    USER = "user"
    PROJECT = "project"
    APPLICATION = "application"
    WORKSPACE = "workspace"
    TASK = "task"


class MemoryKind(StrEnum):
    PREFERENCE = "preference"
    FACT = "fact"
    DECISION = "decision"
    PROCEDURE = "procedure"
    CONSTRAINT = "constraint"
    RELATIONSHIP = "relationship"


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"
    EXPIRED = "expired"
    REJECTED = "rejected"


class MemorySourceType(StrEnum):
    EXPLICIT_USER = "explicit_user"
    INFERRED = "inferred"
    CODING_CONCLUSION = "coding_conclusion"
    LEGACY_IMPORT = "legacy_import"


class MemoryIndexOperation(StrEnum):
    UPSERT = "upsert"
    DELETE = "delete"


@dataclass(frozen=True, order=True)
class OwnerKey:
    scope: MemoryOwnerScope
    id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", MemoryOwnerScope(self.scope))
        owner_id = str(self.id or "").strip()
        if not owner_id:
            raise ValueError("Memory owner ID is required.")
        object.__setattr__(self, "id", owner_id)


@dataclass(frozen=True)
class MemoryEvidence:
    id: str
    memory_id: str
    source_type: MemorySourceType
    source_ref: str
    session_id: str | None = None
    task_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
    excerpt: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not str(self.id or "").strip():
            raise ValueError("Evidence ID is required.")
        if not str(self.memory_id or "").strip():
            raise ValueError("Evidence memory ID is required.")
        object.__setattr__(self, "source_type", MemorySourceType(self.source_type))
        object.__setattr__(self, "source_ref", str(self.source_ref or "").strip())
        excerpt = str(self.excerpt or "").strip()
        if len(excerpt) > 1000:
            excerpt = excerpt[:1000]
        object.__setattr__(self, "excerpt", excerpt)
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))


@dataclass(frozen=True)
class MemoryItem:
    id: str
    owner_scope: MemoryOwnerScope
    owner_id: str
    kind: MemoryKind
    content: str
    normalized_content: str
    status: MemoryStatus
    confidence: float
    salience: float
    valid_from: datetime
    valid_until: datetime | None = None
    last_confirmed_at: datetime | None = None
    supersedes_id: str | None = None
    version: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.id or "").strip():
            raise ValueError("Memory ID is required.")
        owner = OwnerKey(self.owner_scope, self.owner_id)
        object.__setattr__(self, "owner_scope", owner.scope)
        object.__setattr__(self, "owner_id", owner.id)
        object.__setattr__(self, "kind", MemoryKind(self.kind))
        object.__setattr__(self, "status", MemoryStatus(self.status))
        content = str(self.content or "").strip()
        normalized = str(self.normalized_content or "").strip()
        if not content or not normalized:
            raise ValueError("Memory content and normalized content are required.")
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "normalized_content", normalized)
        for name in ("confidence", "salience"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1.")
            object.__setattr__(self, name, value)
        if int(self.version) < 1:
            raise ValueError("Memory version must be positive.")
        object.__setattr__(self, "version", int(self.version))
        valid_from = _aware(self.valid_from, "valid_from")
        valid_until = _aware(self.valid_until, "valid_until") if self.valid_until else None
        if valid_until is not None and valid_until <= valid_from:
            raise ValueError("valid_until must be later than valid_from.")
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "valid_until", valid_until)
        if self.last_confirmed_at is not None:
            object.__setattr__(
                self,
                "last_confirmed_at",
                _aware(self.last_confirmed_at, "last_confirmed_at"),
            )
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _aware(self.updated_at, "updated_at"))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @property
    def owner(self) -> OwnerKey:
        return OwnerKey(self.owner_scope, self.owner_id)

    def is_retrievable(self, now: datetime | None = None) -> bool:
        current = _aware(now or utc_now(), "now")
        return (
            self.status is MemoryStatus.ACTIVE
            and self.valid_from <= current
            and (self.valid_until is None or self.valid_until > current)
        )

    def transitioned(
        self,
        status: MemoryStatus,
        *,
        now: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "MemoryItem":
        target = MemoryStatus(status)
        validate_transition(self.status, target)
        changed_at = _aware(now or utc_now(), "now")
        merged_metadata = dict(self.metadata)
        merged_metadata.update(metadata or {})
        return replace(
            self,
            status=target,
            version=self.version + 1,
            updated_at=changed_at,
            last_confirmed_at=(
                changed_at if target is MemoryStatus.ACTIVE else self.last_confirmed_at
            ),
            metadata=merged_metadata,
        )


ALLOWED_TRANSITIONS: dict[MemoryStatus, frozenset[MemoryStatus]] = {
    MemoryStatus.CANDIDATE: frozenset({
        MemoryStatus.ACTIVE,
        MemoryStatus.REJECTED,
        MemoryStatus.REVOKED,
        MemoryStatus.EXPIRED,
    }),
    MemoryStatus.ACTIVE: frozenset({
        MemoryStatus.SUPERSEDED,
        MemoryStatus.REVOKED,
        MemoryStatus.EXPIRED,
    }),
    MemoryStatus.SUPERSEDED: frozenset(),
    MemoryStatus.REVOKED: frozenset(),
    MemoryStatus.EXPIRED: frozenset(),
    MemoryStatus.REJECTED: frozenset(),
}


def validate_transition(current: MemoryStatus, target: MemoryStatus) -> None:
    source = MemoryStatus(current)
    destination = MemoryStatus(target)
    if destination not in ALLOWED_TRANSITIONS[source]:
        raise ValueError(f"Invalid memory transition: {source.value} -> {destination.value}")


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")
    return value
