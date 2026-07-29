from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from runtime.context.events import ContextEvent, ContextEventType, thaw
from runtime.sessions.session_store import SessionStore

if TYPE_CHECKING:
    from runtime.context.long_content import LongContentDetector


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Session:
    id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    active_agent: str = "hybrid"
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    last_compacted: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # ``messages`` remains the legacy chat transport.  These three values have
    # deliberately different lifetimes and must never alias one another.
    event_log: list[ContextEvent] = field(default_factory=list)
    active_event_window: list[ContextEvent] = field(default_factory=list, init=False)
    prompt_messages: list[dict[str, Any]] = field(default_factory=list)
    archive_boundary_seq: int = 0
    checkpoints: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.messages = [dict(message) for message in self.messages]
        self.prompt_messages = [dict(message) for message in self.prompt_messages]
        self.event_log = [
            event if isinstance(event, ContextEvent) else ContextEvent.from_dict(event)
            for event in self.event_log
        ]
        self.event_log.sort(key=lambda event: event.seq)
        self._validate_event_log()
        self.archive_boundary_seq = max(0, int(self.archive_boundary_seq or 0))
        self._backfill_legacy_messages()
        self._refresh_active_event_window()

    def __deepcopy__(self, memo: dict[int, Any]) -> "Session":
        """Copy durable session state without copying runtime-only adapters.

        Context events intentionally freeze their payloads with
        ``MappingProxyType``.  Python's generic ``deepcopy`` cannot copy that
        type, and the session manager also attaches a bound checkpoint
        persister that should never be sent to a background worker.
        """
        existing = memo.get(id(self))
        if existing is not None:
            return existing

        snapshot = type(self)(
            id=self.id,
            messages=deepcopy(thaw(self.messages), memo),
            active_agent=self.active_agent,
            created_at=self.created_at,
            updated_at=self.updated_at,
            last_compacted=self.last_compacted,
            metadata=deepcopy(thaw(self.metadata), memo),
            event_log=[event.to_dict() for event in self.event_log],
            prompt_messages=deepcopy(thaw(self.prompt_messages), memo),
            archive_boundary_seq=self.archive_boundary_seq,
            checkpoints=deepcopy(thaw(self.checkpoints), memo),
        )
        memo[id(self)] = snapshot
        return snapshot

    def add_message(self, role: str, content: Any, **extra: Any) -> None:
        message = {
            "role": role,
            "content": content,
            "timestamp": _now_iso(),
        }
        message.update(extra)
        self.messages.append(message)
        self.append_event(
            _event_type_for_message(message),
            {"message": message},
            created_at=str(message["timestamp"]),
        )
        self.touch()

    def append_event(
        self,
        event_type: ContextEventType | str,
        payload: dict[str, Any] | None = None,
        *,
        created_at: str | None = None,
        event_id: str | None = None,
    ) -> ContextEvent:
        """Append one immutable fact, assigning the next monotonic sequence."""
        event = ContextEvent.create(
            session_id=self.id,
            seq=(self.event_log[-1].seq + 1) if self.event_log else 1,
            event_type=event_type,
            payload=payload,
            created_at=created_at,
            event_id=event_id,
        )
        self.event_log.append(event)
        self._refresh_active_event_window()
        self.touch()
        return event

    def events_after(self, seq: int | None = None) -> list[ContextEvent]:
        """Return immutable facts strictly after a sequence boundary."""
        self._backfill_legacy_messages()
        boundary = self.archive_boundary_seq if seq is None else max(0, int(seq))
        replaced = self._replaced_event_ids()
        return [
            event
            for event in self.event_log
            if event.seq > boundary and event.event_id not in replaced
        ]

    def set_archive_boundary(self, seq: int) -> int:
        """Archive through ``seq`` without splitting a tool call/result pair."""
        requested = max(0, int(seq))
        if self.event_log:
            requested = min(requested, self.event_log[-1].seq)
        boundary = self._safe_archive_boundary(requested)
        if boundary < self.archive_boundary_seq:
            raise ValueError("archive boundary cannot move backwards")
        self.archive_boundary_seq = boundary
        self._refresh_active_event_window()
        self.touch()
        return boundary

    def archive_until(self, seq: int) -> int:
        return self.set_archive_boundary(seq)

    def advance_archive_boundary(self, seq: int) -> int:
        return self.set_archive_boundary(seq)

    def set_prompt_messages(self, messages: list[dict[str, Any]]) -> None:
        """Set the per-model-call prompt transport without retaining it as history."""
        self.prompt_messages = [dict(message) for message in messages]

    def clear_prompt_messages(self) -> None:
        self.prompt_messages.clear()

    @property
    def archive_boundary_event_id(self) -> str | None:
        for event in reversed(self.event_log):
            if event.seq == self.archive_boundary_seq:
                return event.event_id
        return None

    def set_mode(self, mode: str) -> None:
        self.active_agent = mode
        self.touch()

    def selected_agent(self) -> str:
        return self.active_agent

    def mark_compacted(self) -> None:
        self.last_compacted = _now_iso()
        self.touch()

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def _backfill_legacy_messages(self) -> None:
        """Turn untracked legacy chat rows into deterministic immutable events."""
        tracked = []
        for event in self.event_log:
            payload = thaw(event.payload)
            message = payload.get("message")
            if isinstance(message, Mapping):
                tracked.append(dict(message))
        for message_index, message in enumerate(self.messages):
            normalized = dict(message)
            tracked_match = next(
                (index for index, candidate in enumerate(tracked) if candidate == normalized),
                None,
            )
            if tracked_match is not None:
                tracked.pop(tracked_match)
                continue
            created_at = str(normalized.get("timestamp") or _legacy_timestamp(message_index))
            self.append_event(
                _event_type_for_message(normalized),
                {"message": normalized, "legacy_message_index": message_index},
                created_at=created_at,
            )

    def _validate_event_log(self) -> None:
        previous = 0
        event_ids: set[str] = set()
        for event in self.event_log:
            if event.session_id != self.id:
                raise ValueError("context event belongs to a different session")
            if event.seq <= previous:
                raise ValueError("context event seq must be strictly monotonic")
            if event.event_id in event_ids:
                raise ValueError("context event_id must be unique within a session")
            previous = event.seq
            event_ids.add(event.event_id)

    def _safe_archive_boundary(self, requested: int) -> int:
        boundary = requested
        while boundary:
            moved = False
            for event in self.event_log:
                if event.seq > boundary or event.type != ContextEventType.TOOL_CALL.value:
                    continue
                result_seq = _tool_result_seq(event, self.event_log)
                if result_seq is None or result_seq > boundary:
                    boundary = event.seq - 1
                    moved = True
                    break
            if not moved:
                return boundary
        return 0

    def _refresh_active_event_window(self) -> None:
        replaced = self._replaced_event_ids()
        self.active_event_window = [
            event
            for event in self.event_log
            if event.seq > self.archive_boundary_seq and event.event_id not in replaced
        ]

    def _replaced_event_ids(self) -> set[str]:
        replaced: set[str] = set()
        for event in self.event_log:
            if event.type != ContextEventType.LEGACY_MESSAGE_REPLACED.value:
                continue
            payload = thaw(event.payload)
            event_id = str(payload.get("replaces_event_id") or "")
            if event_id:
                replaced.add(event_id)
        return replaced


class SessionManager:
    def __init__(
        self,
        database_url: str | Path | None = None,
        *,
        max_sessions: int = 128,
        long_content_detector: "LongContentDetector | None" = None,
    ) -> None:
        self.max_sessions = max(1, int(max_sessions))
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._store = SessionStore(database_url)
        self._long_content_detector = long_content_detector

    def get_or_create(self, session_id: str) -> Session:
        if session_id in self._sessions:
            self._sessions.move_to_end(session_id)
        else:
            loaded = self._store.load_session(session_id)
            if loaded is None:
                session = Session(id=session_id)
            else:
                messages, messages_changed, artifact_facts, replacement_facts = (
                    _externalize_untracked_legacy_messages(
                        loaded.get("messages", []),
                        event_log=loaded.get("event_log", []),
                        detector=self._long_content_detector,
                        session_id=str(loaded["id"]),
                    )
                )
                session = Session(
                    id=loaded["id"],
                    messages=messages,
                    active_agent=loaded["active_agent"],
                    created_at=loaded["created_at"],
                    updated_at=loaded["updated_at"],
                    last_compacted=loaded["last_compacted"],
                    metadata=loaded["metadata"],
                    event_log=loaded.get("event_log", []),
                    archive_boundary_seq=loaded.get("archive_boundary_seq", 0),
                    checkpoints=loaded.get("checkpoints", []),
                )
                for fact in artifact_facts:
                    session.append_event(
                        ContextEventType.ARTIFACT_CREATED,
                        {
                            "artifact_ref": fact["artifact_ref"],
                            "source": "legacy_session_message",
                            "legacy_message_index": fact["message_index"],
                        },
                        created_at=fact["created_at"],
                    )
                for fact in replacement_facts:
                    session.append_event(
                        ContextEventType.LEGACY_MESSAGE_REPLACED,
                        {
                            "replaces_event_id": fact["replaces_event_id"],
                            "replacement_event_id": _legacy_message_event_id(
                                session, fact["message_index"]
                            ),
                            "artifact_ref": fact["artifact_ref"],
                            "source": "legacy_session_message",
                            "legacy_message_index": fact["message_index"],
                        },
                        created_at=fact["created_at"],
                    )
                if messages_changed or artifact_facts or replacement_facts:
                    # Artifact publication happens before the source row is
                    # replaced. A failed save therefore leaves the legacy row
                    # retryable, while content addressing prevents duplicates.
                    self._store.save_session(session)
            self._bind_checkpoint_persister(session)
            self._sessions[session_id] = session
            self._evict_if_needed()
        return self._sessions[session_id]

    def save(self, session: Session) -> None:
        self._bind_checkpoint_persister(session)
        session.touch()
        self._sessions[session.id] = session
        self._sessions.move_to_end(session.id)
        self._evict_if_needed()
        self._store.save_session(session)

    def compact(
        self,
        session: Session,
        *,
        checkpoint: dict[str, Any],
        archive_boundary_seq: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._bind_checkpoint_persister(session)
        result = self._store.compact_session(
            session,
            checkpoint=checkpoint,
            archive_boundary_seq=archive_boundary_seq,
            metadata=metadata,
        )
        self._sessions[session.id] = session
        self._sessions.move_to_end(session.id)
        self._evict_if_needed()
        return result

    def list_sessions(self) -> list[dict[str, Any]]:
        return self._store.list_sessions()

    def delete(self, session_id: str) -> bool:
        self._sessions.pop(session_id, None)
        return self._store.delete_session(session_id)

    def cleanup_expired_sessions(
        self,
        *,
        max_age_days: int,
        now: datetime | None = None,
    ) -> int:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max(0, int(max_age_days)))
        removed = 0
        for row in self.list_sessions():
            updated_at = _parse_iso_datetime(row.get("updated_at"))
            if updated_at is None or updated_at >= cutoff:
                continue
            if self.delete(str(row.get("id", ""))):
                removed += 1
        return removed

    def close(self) -> None:
        self._store.close()

    def _evict_if_needed(self) -> None:
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)

    def _bind_checkpoint_persister(self, session: Session) -> None:
        # Runtime-only adapter used by lazy state migrations. It is not part of
        # Session metadata and is therefore never serialized as task state.
        session._context_checkpoint_persister = self.compact


def _externalize_untracked_legacy_messages(
    messages: list[dict[str, Any]],
    *,
    event_log: list[ContextEvent | Mapping[str, Any]],
    detector: "LongContentDetector | None",
    session_id: str,
) -> tuple[
    list[dict[str, Any]],
    bool,
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Externalize legacy rows before Session can backfill them as events.

    Rows already represented by an immutable event are left untouched. This
    migration only changes old transport rows that would otherwise create new
    events during ``Session.__post_init__``.
    """
    copied = [dict(message) for message in messages]
    if detector is None:
        return copied, False, [], []

    tracked_messages: list[tuple[dict[str, Any], ContextEvent]] = []
    recorded_artifacts: set[str] = set()
    for raw_event in event_log:
        event = (
            raw_event
            if isinstance(raw_event, ContextEvent)
            else ContextEvent.from_dict(raw_event)
        )
        payload = thaw(event.payload)
        if event.type == ContextEventType.ARTIFACT_CREATED.value:
            artifact_ref = payload.get("artifact_ref")
            artifact_key = _artifact_ref_key(artifact_ref)
            if artifact_key:
                recorded_artifacts.add(artifact_key)
        message = payload.get("message")
        if isinstance(message, Mapping):
            tracked_messages.append((dict(message), event))

    changed = False
    artifact_facts: list[dict[str, Any]] = []
    replacement_facts: list[dict[str, Any]] = []
    for message_index, message in enumerate(copied):
        tracked_match = next(
            (
                index
                for index, (candidate, _event) in enumerate(tracked_messages)
                if candidate == message
            ),
            None,
        )
        tracked_event = None
        if tracked_match is not None:
            _tracked_message, tracked_event = tracked_messages.pop(tracked_match)
        content = message.get("content")
        if not isinstance(content, (str, bytes, Mapping, list)):
            continue
        existing_artifact_ref = _message_artifact_ref(message)
        metadata = message.get("metadata")
        already_externalized = (
            existing_artifact_ref is not None
            and isinstance(metadata, Mapping)
            and bool(metadata.get("content_externalized"))
        )
        if already_externalized:
            artifact_key = _artifact_ref_key(existing_artifact_ref)
            if artifact_key not in recorded_artifacts:
                artifact_facts.append(
                    _legacy_artifact_fact(
                        message, message_index, existing_artifact_ref
                    )
                )
                recorded_artifacts.add(artifact_key)
            continue
        result = detector.externalize(
            content,
            artifact_type="legacy_message",
            name=f"legacy-session-message-{message_index}",
            metadata={
                "source": "legacy_session_message",
                "session_id": session_id,
                "message_index": message_index,
                "role": str(message.get("role") or ""),
            },
        )
        artifact_ref = (
            result.artifact_ref.to_dict()
            if result.artifact_ref is not None
            else _message_artifact_ref(message)
        )
        if artifact_ref is None:
            continue
        needs_replacement = result.externalized or _artifact_ref_needs_normalization(
            message
        )
        if tracked_event is not None and not needs_replacement:
            artifact_key = _artifact_ref_key(artifact_ref)
            if artifact_key not in recorded_artifacts:
                artifact_facts.append(
                    _legacy_artifact_fact(message, message_index, artifact_ref)
                )
                recorded_artifacts.add(artifact_key)
            continue
        if result.externalized:
            message["content"] = result.content
        metadata = (
            dict(message.get("metadata"))
            if isinstance(message.get("metadata"), Mapping)
            else {}
        )
        metadata["artifact_ref"] = artifact_ref
        metadata["content_externalized"] = True
        if result.externalized:
            metadata["original_content_tokens"] = result.assessment.token_count
            metadata["original_content_chars"] = result.assessment.char_count
            metadata["original_content_bytes"] = result.assessment.byte_count
        message["metadata"] = metadata
        message.pop("artifact_ref", None)
        changed = True
        if tracked_event is not None:
            replacement_facts.append({
                **_legacy_artifact_fact(message, message_index, artifact_ref),
                "replaces_event_id": tracked_event.event_id,
            })
        artifact_key = _artifact_ref_key(artifact_ref)
        if artifact_key not in recorded_artifacts:
            artifact_facts.append(
                _legacy_artifact_fact(message, message_index, artifact_ref)
            )
            recorded_artifacts.add(artifact_key)
    return copied, changed, artifact_facts, replacement_facts


def _message_artifact_ref(message: Mapping[str, Any]) -> dict[str, Any] | None:
    metadata = message.get("metadata")
    nested = metadata.get("artifact_ref") if isinstance(metadata, Mapping) else None
    value = nested if isinstance(nested, Mapping) else message.get("artifact_ref")
    return dict(value) if isinstance(value, Mapping) else None


def _artifact_ref_key(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    return str(value.get("artifact_id") or value.get("storage_uri") or "")


def _artifact_ref_needs_normalization(message: Mapping[str, Any]) -> bool:
    metadata = message.get("metadata")
    nested = metadata.get("artifact_ref") if isinstance(metadata, Mapping) else None
    return not isinstance(nested, Mapping) and isinstance(
        message.get("artifact_ref"), Mapping
    )


def _legacy_artifact_fact(
    message: Mapping[str, Any],
    message_index: int,
    artifact_ref: dict[str, Any],
) -> dict[str, Any]:
    return {
        "artifact_ref": dict(artifact_ref),
        "message_index": message_index,
        "created_at": str(message.get("timestamp") or _legacy_timestamp(message_index)),
    }


def _legacy_message_event_id(session: Session, message_index: int) -> str:
    for event in reversed(session.event_log):
        payload = thaw(event.payload)
        if payload.get("legacy_message_index") != message_index:
            continue
        if isinstance(payload.get("message"), Mapping):
            return event.event_id
    return ""


def _parse_iso_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _legacy_timestamp(index: int) -> str:
    # Stable backfill ids require a timestamp even for pre-event legacy rows.
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index)).isoformat()


def _event_type_for_message(message: dict[str, Any]) -> ContextEventType:
    role = str(message.get("role") or "")
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    kind = str(metadata.get("kind") or message.get("kind") or "")
    if role == "user":
        return ContextEventType.USER_CORRECTION if kind in {"correction", "user_correction"} else ContextEventType.USER_MESSAGE
    if role == "tool":
        return ContextEventType.TOOL_RESULT
    if role == "assistant" and (message.get("tool_calls") or kind == "tool_call"):
        return ContextEventType.TOOL_CALL
    if kind in {"runtime_error", "error"}:
        return ContextEventType.RUNTIME_ERROR
    return ContextEventType.ASSISTANT_MESSAGE


def _tool_call_ids(event: ContextEvent) -> set[str]:
    payload = thaw(event.payload)
    source = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    identifiers = {str(source[key]) for key in ("call_id", "tool_call_id", "id") if source.get(key)}
    for call in source.get("tool_calls", []) if isinstance(source.get("tool_calls"), list) else []:
        if isinstance(call, dict) and call.get("id"):
            identifiers.add(str(call["id"]))
    return identifiers


def _tool_result_seq(call: ContextEvent, events: list[ContextEvent]) -> int | None:
    call_ids = _tool_call_ids(call)
    for event in events:
        if event.seq <= call.seq or event.type != ContextEventType.TOOL_RESULT.value:
            continue
        result_ids = _tool_call_ids(event)
        if call_ids and call_ids.intersection(result_ids):
            return event.seq
        if not call_ids and event.seq == call.seq + 1:
            return event.seq
    return None
