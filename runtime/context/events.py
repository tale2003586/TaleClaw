"""Immutable, replayable facts used to reconstruct a session context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping


class ContextEventType(str, Enum):
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    RUNTIME_ERROR = "runtime_error"
    USER_CORRECTION = "user_correction"
    TASK_STATE_CHECKPOINT = "task_state_checkpoint"
    RUN_CHECKPOINT = "run_checkpoint"
    SUBAGENTS_DISPATCHED = "subagents_dispatched"
    SUBAGENT_RESULTS = "subagent_results"
    SUBAGENTS_COMPLETED = "subagents_completed"
    COMPACTION_COMPLETED = "compaction_completed"
    ARTIFACT_CREATED = "artifact_created"
    LEGACY_MESSAGE_REPLACED = "legacy_message_replaced"


EVENT_TYPES = frozenset(item.value for item in ContextEventType)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


def thaw(value: Any) -> Any:
    """Return a JSON-compatible copy of a frozen event value."""
    if isinstance(value, Mapping):
        return {str(key): thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        thaw(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def payload_checksum(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def stable_event_id(
    *,
    session_id: str,
    seq: int,
    event_type: str,
    created_at: str,
    payload: Any,
) -> str:
    """Derive an id from immutable event facts, not a process-local UUID."""
    material = canonical_json({
        "session_id": session_id,
        "seq": int(seq),
        "event_type": event_type,
        "created_at": created_at,
        "payload": payload,
    })
    return "evt_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class ContextEvent(Mapping[str, Any]):
    """One append-only fact in a session's context event log."""

    event_id: str
    session_id: str
    seq: int
    event_type: ContextEventType | str
    created_at: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        event_type = str(self.event_type.value if isinstance(self.event_type, ContextEventType) else self.event_type)
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported context event type: {event_type}")
        if not str(self.session_id):
            raise ValueError("context event session_id is required")
        if int(self.seq) < 1:
            raise ValueError("context event seq must be positive")
        if not str(self.event_id):
            raise ValueError("context event event_id is required")
        object.__setattr__(self, "seq", int(self.seq))
        object.__setattr__(self, "event_type", ContextEventType(event_type))
        object.__setattr__(self, "payload", _freeze(dict(self.payload or {})))

    @property
    def type(self) -> str:
        return self.event_type.value

    @property
    def payload_sha256(self) -> str:
        return payload_checksum(self.payload)

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        seq: int,
        event_type: ContextEventType | str,
        payload: Mapping[str, Any] | None = None,
        created_at: str | None = None,
        event_id: str | None = None,
    ) -> "ContextEvent":
        rendered_type = event_type.value if isinstance(event_type, ContextEventType) else str(event_type)
        rendered_payload = dict(payload or {})
        rendered_at = created_at or _now_iso()
        return cls(
            event_id=event_id or stable_event_id(
                session_id=session_id,
                seq=seq,
                event_type=rendered_type,
                created_at=rendered_at,
                payload=rendered_payload,
            ),
            session_id=session_id,
            seq=seq,
            event_type=rendered_type,
            created_at=rendered_at,
            payload=rendered_payload,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ContextEvent":
        return cls.create(
            event_id=str(data.get("event_id") or ""),
            session_id=str(data.get("session_id") or ""),
            seq=int(data.get("seq") or 0),
            event_type=str(data.get("event_type", data.get("type", ""))),
            created_at=str(data.get("created_at") or _now_iso()),
            payload=data.get("payload") if isinstance(data.get("payload"), Mapping) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "seq": self.seq,
            "event_type": self.type,
            "created_at": self.created_at,
            "payload": thaw(self.payload),
            "payload_sha256": self.payload_sha256,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self):
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


__all__ = (
    "ContextEvent",
    "ContextEventType",
    "EVENT_TYPES",
    "canonical_json",
    "payload_checksum",
    "stable_event_id",
    "thaw",
)
