from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from memory.dedup import normalize_memory_text
from memory.domain import MemoryItem, MemoryKind, MemoryOwnerScope, MemoryStatus


class MemoryNoteStatus(StrEnum):
    PENDING = "pending"
    STABLE = "stable"
    ARCHIVED = "archived"
    REJECTED = "rejected"


class MemoryNoteOrigin(StrEnum):
    EXPLICIT_USER = "explicit_user"
    INFERRED_BY_LLM = "inferred_by_llm"
    TOOL_RESULT = "tool_result"
    TASK_SUMMARY = "task_summary"
    SYSTEM_EVENT = "system_event"
    LEGACY_IMPORT = "legacy_import"


class MemoryRelationType(StrEnum):
    RELATED_TO = "related_to"
    SUPPORTS = "supports"
    UPDATES = "updates"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"
    DERIVED_FROM = "derived_from"
    ENRICHES = "enriches"
    DUPLICATE_OF = "duplicate_of"


class MemoryLinkCreator(StrEnum):
    LLM = "llm"
    RUNTIME = "runtime"
    USER = "user"


class MemoryLinkStatus(StrEnum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class MemoryNote:
    id: str
    content: str
    memory_type: MemoryKind
    scope: MemoryOwnerScope
    scope_id: str
    contextual_description: str = ""
    keywords: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    source: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.5
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_accessed_at: datetime | None = None
    access_count: int = 0
    status: MemoryNoteStatus = MemoryNoteStatus.PENDING
    origin: MemoryNoteOrigin = MemoryNoteOrigin.INFERRED_BY_LLM
    links: tuple[str, ...] = ()
    audit_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("id", "content", "scope_id"):
            value = str(getattr(self, name) or "").strip()
            if not value:
                raise ValueError(f"MemoryNote {name} is required.")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "memory_type", MemoryKind(self.memory_type))
        object.__setattr__(self, "scope", MemoryOwnerScope(self.scope))
        object.__setattr__(self, "status", MemoryNoteStatus(self.status))
        object.__setattr__(self, "origin", MemoryNoteOrigin(self.origin))
        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("MemoryNote confidence must be between 0 and 1.")
        object.__setattr__(self, "confidence", confidence)
        if int(self.access_count) < 0:
            raise ValueError("MemoryNote access_count cannot be negative.")
        object.__setattr__(self, "access_count", int(self.access_count))
        object.__setattr__(self, "keywords", _clean_values(self.keywords, limit=16))
        object.__setattr__(self, "tags", _clean_values(self.tags, limit=16))
        object.__setattr__(self, "links", _clean_values(self.links, limit=64))
        object.__setattr__(self, "source", dict(self.source or {}))
        object.__setattr__(self, "audit_metadata", dict(self.audit_metadata or {}))
        for name in ("created_at", "updated_at", "last_accessed_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware(value, name))

    @classmethod
    def from_legacy(cls, item: MemoryItem) -> "MemoryNote":
        return cls(
            id=item.id,
            content=item.content,
            contextual_description=str(item.metadata.get("contextual_description") or ""),
            memory_type=item.kind,
            keywords=tuple(item.metadata.get("keywords") or ()),
            tags=tuple(item.metadata.get("tags") or ()),
            scope=item.owner_scope,
            scope_id=item.owner_id,
            source={"source_session_id": item.metadata.get("source_session_id")},
            confidence=item.confidence,
            created_at=item.created_at,
            updated_at=item.updated_at,
            last_accessed_at=_optional_datetime(item.metadata.get("last_accessed_at")),
            access_count=int(item.metadata.get("access_count") or 0),
            status=_note_status(item.status),
            origin=_note_origin(item.metadata.get("origin")),
            links=tuple(item.metadata.get("links") or ()),
            audit_metadata={
                "legacy_metadata": dict(item.metadata),
                "salience": item.salience,
                "valid_from": item.valid_from,
                "valid_until": item.valid_until,
                "last_confirmed_at": item.last_confirmed_at,
                "supersedes_id": item.supersedes_id,
                "version": item.version,
                "legacy_status": item.status.value,
            },
        )

    def to_legacy(self) -> MemoryItem:
        legacy = dict(self.audit_metadata.get("legacy_metadata") or {})
        legacy.update({
            "contextual_description": self.contextual_description,
            "keywords": list(self.keywords),
            "tags": list(self.tags),
            "origin": self.origin.value,
            "links": list(self.links),
            "access_count": self.access_count,
            "last_accessed_at": (
                self.last_accessed_at.isoformat() if self.last_accessed_at else None
            ),
        })
        return MemoryItem(
            id=self.id,
            owner_scope=self.scope,
            owner_id=self.scope_id,
            kind=self.memory_type,
            content=self.content,
            normalized_content=normalize_memory_text(self.content),
            status=_legacy_status(self.status, self.audit_metadata.get("legacy_status")),
            confidence=self.confidence,
            salience=float(self.audit_metadata.get("salience", 0.5)),
            valid_from=self.audit_metadata.get("valid_from") or self.created_at,
            valid_until=self.audit_metadata.get("valid_until"),
            last_confirmed_at=self.audit_metadata.get("last_confirmed_at"),
            supersedes_id=self.audit_metadata.get("supersedes_id"),
            version=int(self.audit_metadata.get("version") or 1),
            created_at=self.created_at,
            updated_at=self.updated_at,
            metadata=legacy,
        )

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass(frozen=True)
class MemoryLink:
    source_memory_id: str
    target_memory_id: str
    relation_type: MemoryRelationType
    confidence: float
    reason: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: MemoryLinkCreator = MemoryLinkCreator.RUNTIME
    status: MemoryLinkStatus = MemoryLinkStatus.CANDIDATE
    audit_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        source = str(self.source_memory_id or "").strip()
        target = str(self.target_memory_id or "").strip()
        if not source or not target or source == target:
            raise ValueError("MemoryLink requires distinct source and target IDs.")
        object.__setattr__(self, "source_memory_id", source)
        object.__setattr__(self, "target_memory_id", target)
        object.__setattr__(self, "relation_type", MemoryRelationType(self.relation_type))
        object.__setattr__(self, "created_by", MemoryLinkCreator(self.created_by))
        object.__setattr__(self, "status", MemoryLinkStatus(self.status))
        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("MemoryLink confidence must be between 0 and 1.")
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "reason", str(self.reason or "").strip()[:500])
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        object.__setattr__(self, "audit_metadata", dict(self.audit_metadata or {}))

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


def _clean_values(values, *, limit: int) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value).strip()[:120] for value in values or () if str(value).strip()))[:limit]


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")
    return value


def _optional_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value:
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return None


def _note_status(status: MemoryStatus) -> MemoryNoteStatus:
    return {
        MemoryStatus.CANDIDATE: MemoryNoteStatus.PENDING,
        MemoryStatus.ACTIVE: MemoryNoteStatus.STABLE,
        MemoryStatus.REJECTED: MemoryNoteStatus.REJECTED,
    }.get(status, MemoryNoteStatus.ARCHIVED)


def _legacy_status(status: MemoryNoteStatus, original=None) -> MemoryStatus:
    if status is MemoryNoteStatus.ARCHIVED and original:
        try:
            return MemoryStatus(original)
        except ValueError:
            pass
    return {
        MemoryNoteStatus.PENDING: MemoryStatus.CANDIDATE,
        MemoryNoteStatus.STABLE: MemoryStatus.ACTIVE,
        MemoryNoteStatus.REJECTED: MemoryStatus.REJECTED,
        MemoryNoteStatus.ARCHIVED: MemoryStatus.SUPERSEDED,
    }[status]


def _note_origin(value) -> MemoryNoteOrigin:
    try:
        return MemoryNoteOrigin(value)
    except (TypeError, ValueError):
        return MemoryNoteOrigin.INFERRED_BY_LLM


def _serialize(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value
