"""TaskState prompt rendering and event-window compaction for coding sessions.

`CodingContextState` is a read-only checkpoint snapshot. All mutable task facts
live in :mod:`applications.coding.task_state`.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import time
from typing import Any, Callable, Sequence

from config import (
    CONTEXT_PRESSURE_WINDOW_TOKENS,
    PROMPT_COMPACTION_TARGET_RATIO,
    PROMPT_SOFT_COMPACTION_RATIO,
    SEMANTIC_COMPACTION_ENABLED,
)
from runtime.context.dynamic_budget import PromptBudgetExceeded, select_complete_groups
from runtime.context.events import ContextEvent, ContextEventType, payload_checksum, thaw
from runtime.token_estimator import estimate_tokens

from .compaction import (
    CompactionCoordinator,
    CompactionError,
    ContextCheckpoint,
    SemanticCompactor,
    StateValidationError,
)
from .task_state import (
    Objective,
    TaskState,
    ensure_task_state,
    load_task_state,
    save_task_state,
)


CODING_CONTEXT_STATE_METADATA_KEY = "coding_context_state"
CODING_CONTEXT_STATE_VERSION = 2
DEFAULT_COMPACTION_TRIGGER_TOKENS = 0  # deprecated compatibility value
DEFAULT_COMPACTION_TARGET_TOKENS = 0  # deprecated compatibility value
DEFAULT_RECENT_GROUPS = 0  # deprecated; selection is token-driven


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MessageGroup:
    start: int
    end: int
    messages: list[dict[str, Any]]
    kind: str
    closed: bool = True


@dataclass(frozen=True)
class CodingContextState:
    """Small renderer/checkpoint metadata, never a second task state."""

    version: int = CODING_CONTEXT_STATE_VERSION
    task_id: str = ""
    task_state_version: int = 1
    generation: int = 0
    compacted_until_event_id: str = ""
    source_event_start_id: str = ""
    source_event_end_id: str = ""
    prompt_tail_start_index: int = 0
    compacted_until_index: int = 0
    last_compaction: dict[str, Any] | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=_now)

    @classmethod
    def from_payload(cls, payload: Any) -> "CodingContextState | None":
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return None
        if not isinstance(payload, dict) or int(payload.get("version") or 0) < 2:
            return None
        fields = cls.__dataclass_fields__
        return cls(**{key: value for key, value in payload.items() if key in fields})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CodingContextView:
    state: CodingContextState
    task_state: TaskState
    state_message: dict[str, Any]
    recent_messages: list[dict[str, Any]]
    active_messages: list[dict[str, Any]]
    compacted: bool
    before_tokens: int
    after_tokens: int
    reduction: dict[str, Any] | None


def load_coding_context_state(session) -> CodingContextState | None:
    metadata = getattr(session, "metadata", {}) or {}
    return CodingContextState.from_payload(metadata.get(CODING_CONTEXT_STATE_METADATA_KEY))


def save_coding_context_state(session, state: CodingContextState) -> CodingContextState:
    metadata = getattr(session, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
        session.metadata = metadata
    metadata[CODING_CONTEXT_STATE_METADATA_KEY] = state.to_dict()
    touch = getattr(session, "touch", None)
    if callable(touch):
        touch()
    return state


def build_coding_context_view(
    session,
    *,
    objective: str,
    active_turn_start_index: int | None,
    static_messages: list[dict[str, Any]],
    threshold_tokens: int = 0,
    target_tokens: int = 0,
    keep_recent_groups: int = 0,
    usable_input_tokens: int | None = None,
    semantic_compactor: SemanticCompactor | None = None,
    compaction_persister: Callable[..., Any] | None = None,
) -> CodingContextView:
    """Build one ephemeral prompt view from TaskState and the active event tail.

    Fixed threshold/target/recent arguments remain accepted so older callers do
    not break, but they are intentionally ignored.
    """

    del threshold_tokens, target_tokens, keep_recent_groups
    backfill = getattr(session, "_backfill_legacy_messages", None)
    if callable(backfill):
        backfill()
    messages = [dict(item) for item in (getattr(session, "messages", []) or [])]
    start = max(0, min(int(active_turn_start_index or 0), len(messages)))
    previous_snapshot = load_coding_context_state(session)
    tail_start = max(
        start,
        min(
            int(previous_snapshot.prompt_tail_start_index or 0)
            if previous_snapshot is not None
            else 0,
            len(messages),
        ),
    )
    pinned_user = _latest_real_user_message(messages)
    active_messages = _pin_latest_user(messages[tail_start:], pinned_user)
    event_ref = _latest_real_user_event_ref(session)
    artifact_refs = _latest_user_artifact_refs(messages)
    task_state = ensure_task_state(
        session,
        objective_summary=_objective_summary(objective),
        original_request_ref=event_ref,
        artifact_refs=artifact_refs,
    )
    task_state = _apply_user_objective_change(
        session,
        task_state,
        objective=objective,
        event_ref=event_ref,
        artifact_refs=artifact_refs,
    )
    snapshot = _snapshot(session, task_state, prompt_tail_start_index=tail_start)
    state_message = render_coding_context_state_message(task_state, snapshot=snapshot)
    groups = group_messages(messages, start_index=tail_start)
    recent_messages = _pin_latest_user(_flatten(groups), pinned_user)
    before_tokens = estimate_tokens([*static_messages, state_message, *recent_messages])

    if usable_input_tokens is None:
        available = max(
            1,
            CONTEXT_PRESSURE_WINDOW_TOKENS - estimate_tokens(static_messages),
        )
    else:
        dynamic_static = [
            message
            for message in static_messages
            if str(message.get("role") or "") != "system"
        ]
        available = max(
            1,
            int(usable_input_tokens) - estimate_tokens(dynamic_static),
        )
    soft_trigger = max(1, int(available * PROMPT_SOFT_COMPACTION_RATIO))
    target = max(1, int(available * PROMPT_COMPACTION_TARGET_RATIO))
    compacted = False
    reduction: dict[str, Any] | None = None
    compaction_duration_ms = 0

    if before_tokens > soft_trigger and len(groups) > 1:
        flattened_groups = _flatten(groups)
        pinned_tokens = (
            estimate_tokens([pinned_user])
            if pinned_user is not None and pinned_user not in flattened_groups
            else 0
        )
        selected = _select_recent_groups(
            groups,
            max_tokens=max(
                1,
                target - estimate_tokens([state_message]) - pinned_tokens,
            ),
        )
        kept_start = _first_selected_index(groups, selected)
        if kept_start > tail_start:
            compaction_started = time.perf_counter()
            result = _compact_events(
                session,
                task_state=task_state,
                before_message_index=kept_start,
                semantic_compactor=semantic_compactor,
                compaction_persister=compaction_persister,
            )
            compaction_duration_ms = max(
                0,
                int((time.perf_counter() - compaction_started) * 1000),
            )
            if result is not None:
                task_state, checkpoint = result
                recent_messages = _pin_latest_user(selected, pinned_user)
                snapshot = _snapshot(
                    session,
                    task_state,
                    prompt_tail_start_index=kept_start,
                    checkpoint=checkpoint,
                )
                state_message = render_coding_context_state_message(
                    task_state, snapshot=snapshot
                )
                compacted = True
                reduction = {
                    "section": "coding_context_state",
                    "reason": "task_state_semantic_compaction",
                    "before_tokens": before_tokens,
                    "after_tokens": estimate_tokens([
                        *static_messages, state_message, *recent_messages
                    ]),
                    "soft_trigger_tokens": soft_trigger,
                    "target_tokens": target,
                    "generation": checkpoint.generation,
                    "compacted_until_event_id": checkpoint.compacted_until_event_id,
                }

    after_tokens = estimate_tokens([*static_messages, state_message, *recent_messages])
    runtime_metrics = (getattr(session, "metadata", {}) or {}).get("context_metrics")
    metrics = {
        **dict(snapshot.metrics or {}),
        **(dict(runtime_metrics) if isinstance(runtime_metrics, dict) else {}),
        "prompt_tokens_before_compaction": before_tokens,
        "prompt_tokens_after_compaction": after_tokens,
        "task_state_tokens": estimate_tokens([state_message]),
        "recent_tail_tokens": estimate_tokens(recent_messages),
        "compaction_generation": snapshot.generation,
        "compacted_event_count": len(
            (snapshot.last_compaction or {}).get("source_event_ids", [])
        ),
        "compaction_duration_ms": compaction_duration_ms,
        "usable_input_tokens": int(
            usable_input_tokens or CONTEXT_PRESSURE_WINDOW_TOKENS
        ),
        "dynamic_context_tokens": available,
        "soft_compaction_trigger_tokens": soft_trigger,
        "compaction_target_tokens": target,
    }
    snapshot = CodingContextState(**{
        **snapshot.to_dict(),
        "metrics": metrics,
        "updated_at": _now(),
    })
    save_task_state(session, task_state)
    save_coding_context_state(session, snapshot)
    return CodingContextView(
        state=snapshot,
        task_state=task_state,
        state_message=render_coding_context_state_message(task_state, snapshot=snapshot),
        recent_messages=recent_messages,
        active_messages=active_messages,
        compacted=compacted,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        reduction=reduction,
    )


def render_coding_context_state_message(
    state: TaskState,
    *,
    snapshot: CodingContextState | None = None,
) -> dict[str, Any]:
    snapshot = snapshot or CodingContextState(task_state_version=state.version)
    payload = state.to_dict()
    # Historical replacement details remain checkpointed but do not consume
    # every prompt. Current superseded status is already represented by items.
    payload.pop("history", None)
    extensions = payload.get("extensions")
    coding = extensions.get("coding") if isinstance(extensions, dict) else None
    if isinstance(coding, dict):
        coding.pop("history", None)
    content = (
        '<coding-context-state source="runtime-generated" trust="context-only" '
        f'instructions="false" version="{state.version}" generation="{snapshot.generation}">\n'
        + json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        + "\n</coding-context-state>"
    )
    return {
        "role": "system",
        "content": content,
        "metadata": {
            "kind": "task_state_context",
            "source": "runtime-generated",
            "instructions": False,
            "task_state_version": state.version,
            "generation": snapshot.generation,
        },
    }


def group_messages(
    messages: list[dict[str, Any]], *, start_index: int = 0
) -> list[MessageGroup]:
    """Group complete user turns and assistant/tool transactions."""
    groups: list[MessageGroup] = []
    index = max(0, int(start_index or 0))
    while index < len(messages):
        begin = index
        first = messages[index] if isinstance(messages[index], dict) else {}
        role = str(first.get("role") or "")
        if role == "user":
            index += 1
            if index < len(messages):
                candidate = messages[index]
                if (
                    isinstance(candidate, dict)
                    and str(candidate.get("role") or "") == "assistant"
                    and not candidate.get("tool_calls")
                ):
                    index += 1
            chunk = [dict(item) for item in messages[begin:index] if isinstance(item, dict)]
            groups.append(MessageGroup(begin, index, chunk, "turn", _group_closed(chunk)))
            continue
        if role == "assistant" and first.get("tool_calls"):
            expected = _tool_call_ids(first.get("tool_calls") or [])
            index += 1
            seen: set[str] = set()
            while index < len(messages):
                candidate = messages[index]
                if not isinstance(candidate, dict) or str(candidate.get("role") or "") != "tool":
                    break
                seen.add(str(candidate.get("tool_call_id") or ""))
                index += 1
                if expected and expected <= seen:
                    break
            chunk = [dict(item) for item in messages[begin:index] if isinstance(item, dict)]
            groups.append(MessageGroup(begin, index, chunk, "tool_transaction", not expected or expected <= seen))
            continue
        index += 1
        groups.append(MessageGroup(begin, index, [dict(first)], role or "message"))
    return groups


def _compact_events(
    session,
    *,
    task_state: TaskState,
    before_message_index: int,
    semantic_compactor: SemanticCompactor | None,
    compaction_persister: Callable[..., Any] | None,
) -> tuple[TaskState, ContextCheckpoint] | None:
    events = _events_before_message_index(session, before_message_index)
    if not events:
        return None
    staged: dict[str, Any] = {}
    coordinator = CompactionCoordinator(
        checkpoint_writer=lambda checkpoint: staged.update(checkpoint=checkpoint),
        completion_writer=lambda event: staged.update(completion=event),
        archive_callback=lambda ids: staged.update(archive_ids=list(ids)),
        semantic_compactor=(
            semantic_compactor
            if SEMANTIC_COMPACTION_ENABLED
            else SemanticCompactor()
        ),
    )
    previous_boundary = _snapshot(session, task_state).compacted_until_event_id or None
    try:
        result = coordinator.compact(
            state=task_state,
            events=events,
            compacted_until_event_id=previous_boundary,
        )
    except CompactionError as exc:
        _record_compaction_failure(session, exc)
        return None
    if result.checkpoint is None:
        return None
    checkpoint_payload = {
        "task_state": result.state.to_dict(),
        "context_checkpoint": result.checkpoint.to_dict(),
    }
    boundary_seq = _event_seq(session, result.compacted_until_event_id)
    previous_metadata = deepcopy(getattr(session, "metadata", {}) or {})
    previous_event_log = list(getattr(session, "event_log", []) or [])
    previous_archive_boundary = int(getattr(session, "archive_boundary_seq", 0) or 0)
    previous_checkpoints = deepcopy(getattr(session, "checkpoints", []) or [])
    previous_last_compacted = getattr(session, "last_compacted", None)
    try:
        save_task_state(session, result.state)
        if compaction_persister is not None:
            compaction_persister(
                session=session,
                checkpoint=checkpoint_payload,
                archive_boundary_seq=boundary_seq,
                metadata={"context_checkpoint": result.checkpoint.to_dict()},
            )
        else:
            session.append_event(
                ContextEventType.TASK_STATE_CHECKPOINT,
                {"checkpoint": result.checkpoint.to_dict()},
            )
            session.append_event(
                ContextEventType.COMPACTION_COMPLETED,
                staged.get("completion") or {},
            )
            session.set_archive_boundary(boundary_seq)
            session.checkpoints.insert(0, {
                "checkpoint_id": f"memory:{result.checkpoint.checksum[:16]}",
                "archive_boundary_seq": boundary_seq,
                "completion_event_id": "",
                "created_at": result.checkpoint.created_at,
                "state": checkpoint_payload,
                "state_sha256": payload_checksum(checkpoint_payload),
                "metadata": {},
            })
    except Exception as exc:
        session.metadata = previous_metadata
        session.event_log = previous_event_log
        session.archive_boundary_seq = previous_archive_boundary
        session.checkpoints = previous_checkpoints
        session.last_compacted = previous_last_compacted
        refresh = getattr(session, "_refresh_active_event_window", None)
        if callable(refresh):
            refresh()
        _record_compaction_failure(session, exc)
        return None
    return result.state, result.checkpoint


def _snapshot(
    session,
    task_state: TaskState,
    *,
    prompt_tail_start_index: int = 0,
    checkpoint: ContextCheckpoint | None = None,
) -> CodingContextState:
    existing = load_coding_context_state(session)
    latest_checkpoint = checkpoint
    if latest_checkpoint is None:
        for item in list(getattr(session, "checkpoints", []) or []):
            payload = item.get("state") if isinstance(item, dict) else None
            raw = payload.get("context_checkpoint") if isinstance(payload, dict) else None
            if isinstance(raw, dict):
                latest_checkpoint = ContextCheckpoint(
                    generation=int(raw.get("generation") or 0),
                    state_version=int(raw.get("state_version") or raw.get("task_state_version") or 1),
                    source_event_ids=[str(value) for value in raw.get("source_event_ids") or []],
                    compacted_until_event_id=str(raw.get("compacted_until_event_id") or ""),
                    artifact_refs=[str(value) for value in raw.get("artifact_refs") or []],
                    checksum=str(raw.get("checksum") or ""),
                    created_at=str(raw.get("created_at") or ""),
                )
                break
    generation = latest_checkpoint.generation if latest_checkpoint else (existing.generation if existing else 0)
    boundary = latest_checkpoint.compacted_until_event_id if latest_checkpoint else (
        str(getattr(session, "archive_boundary_event_id", "") or "")
        or (existing.compacted_until_event_id if existing else "")
    )
    source_ids = latest_checkpoint.source_event_ids if latest_checkpoint else []
    return CodingContextState(
        task_id=str(getattr(session, "id", "") or ""),
        task_state_version=task_state.version,
        generation=generation,
        compacted_until_event_id=boundary,
        source_event_start_id=source_ids[0] if source_ids else "",
        source_event_end_id=source_ids[-1] if source_ids else "",
        prompt_tail_start_index=prompt_tail_start_index,
        compacted_until_index=int(getattr(session, "archive_boundary_seq", 0) or 0),
        last_compaction=(latest_checkpoint.to_dict() if latest_checkpoint else None),
        metrics=dict(existing.metrics or {}) if existing else {},
    )


def _select_recent_groups(groups: list[MessageGroup], *, max_tokens: int) -> list[dict[str, Any]]:
    raw_groups = [group.messages for group in groups]
    required = {len(groups) - 1}
    latest_user_group = _latest_real_user_group(groups)
    if latest_user_group is not None:
        required.add(latest_user_group)
    required.update(index for index, group in enumerate(groups) if not group.closed)
    return select_complete_groups(
        raw_groups,
        max_tokens=max_tokens,
        required_group_indexes=required,
    )


def _first_selected_index(groups: list[MessageGroup], selected: list[dict[str, Any]]) -> int:
    if not selected:
        return groups[-1].start if groups else 0
    selected_firsts = [
        message
        for message in selected
        if isinstance(message, dict)
    ]
    suffix_start = len(groups) - 1
    for index in range(len(groups) - 1, -1, -1):
        first = groups[index].messages[0] if groups[index].messages else None
        if first is None or first not in selected_firsts:
            break
        suffix_start = index
    return groups[suffix_start].start if groups else 0


def _events_before_message_index(session, index: int) -> list[dict[str, Any]]:
    result = []
    archive_boundary = int(getattr(session, "archive_boundary_seq", 0) or 0)
    active_events = getattr(session, "active_event_window", None)
    events = (
        list(active_events)
        if isinstance(active_events, list)
        else [
            event
            for event in list(getattr(session, "event_log", []) or [])
            if event.seq > archive_boundary
        ]
    )
    for event in events:
        payload = thaw(event.payload)
        message_index = payload.get("legacy_message_index")
        message = payload.get("message")
        if message_index is None and isinstance(message, dict):
            message_index = _matching_message_index(session, message)
        if message_index is not None and int(message_index) >= index:
            continue
        result.append(event.to_dict())
    return result


def _matching_message_index(session, target: dict[str, Any]) -> int | None:
    for index, message in enumerate(list(getattr(session, "messages", []) or [])):
        if isinstance(message, dict) and message == target:
            return index
    return None


def _event_seq(session, event_id: str | None) -> int:
    for event in list(getattr(session, "event_log", []) or []):
        if event.event_id == event_id:
            return event.seq
    return int(getattr(session, "archive_boundary_seq", 0) or 0)


def _latest_real_user_event_ref(session) -> str:
    for event in reversed(list(getattr(session, "event_log", []) or [])):
        if event.type in {ContextEventType.USER_MESSAGE.value, ContextEventType.USER_CORRECTION.value}:
            payload = thaw(event.payload)
            message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            if str(metadata.get("source") or "").startswith("runtime-generated"):
                continue
            return f"event://{event.event_id}"
    return ""


def _latest_user_artifact_refs(messages: Sequence[dict[str, Any]]) -> list[str]:
    for message in reversed(messages):
        if str(message.get("role") or "") != "user":
            continue
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        ref = metadata.get("artifact_ref") or message.get("artifact_ref")
        if isinstance(ref, dict):
            uri = ref.get("storage_uri") or ref.get("artifact_id")
            return [str(uri)] if uri else []
        if ref:
            return [str(ref)]
        return []
    return []


def _latest_real_user_message(
    messages: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    for message in reversed(messages):
        if not isinstance(message, dict) or str(message.get("role") or "") != "user":
            continue
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        if str(metadata.get("source") or "").startswith("runtime-generated"):
            continue
        return dict(message)
    return None


def _pin_latest_user(
    messages: Sequence[dict[str, Any]],
    latest_user: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    rendered = [dict(message) for message in messages if isinstance(message, dict)]
    if latest_user is not None and latest_user not in rendered:
        rendered.insert(0, dict(latest_user))
    return rendered


def _latest_real_user_group(groups: Sequence[MessageGroup]) -> int | None:
    for index in range(len(groups) - 1, -1, -1):
        for message in groups[index].messages:
            if str(message.get("role") or "") != "user":
                continue
            metadata = (
                message.get("metadata")
                if isinstance(message.get("metadata"), dict)
                else {}
            )
            if not str(metadata.get("source") or "").startswith("runtime-generated"):
                return index
    return None


def _apply_user_objective_change(
    session,
    state: TaskState,
    *,
    objective: str,
    event_ref: str,
    artifact_refs: list[str],
) -> TaskState:
    if not event_ref or event_ref == state.objective.original_request_ref:
        return state
    state.history.append(_objective_history(state, objective, event_ref))
    state.objective = Objective(
        summary=_objective_summary(objective),
        original_request_ref=event_ref,
        source_artifacts=artifact_refs,
        supersedes=state.objective.original_request_ref,
    )
    state.version += 1
    state.updated_at = _now()
    return save_task_state(session, state)


def _objective_history(state: TaskState, objective: str, event_ref: str):
    from .task_state import StateHistoryEntry

    return StateHistoryEntry(
        id=f"objective:{state.version}:{event_ref}",
        category="objective",
        item_id="objective",
        previous=state.objective.__dict__.copy(),
        replacement={
            "summary": _objective_summary(objective),
            "original_request_ref": event_ref,
        },
    )


def _objective_summary(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "Complete the current coding task"
    first = text.split("\n\n", 1)[0].strip()
    return " ".join(first.split())[:300]


def _record_compaction_failure(session, error: Exception) -> None:
    metadata = getattr(session, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
        session.metadata = metadata
    metrics = metadata.get("context_metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    causes = _exception_chain(error)
    key = (
        "state_patch_validation_failures"
        if any(isinstance(item, StateValidationError) for item in causes)
        else "semantic_compaction_failures"
    )
    metrics[key] = int(metrics.get(key, 0) or 0) + 1
    if any(isinstance(item, PromptBudgetExceeded) for item in causes):
        metrics["hard_budget_blocks"] = int(
            metrics.get("hard_budget_blocks", 0) or 0
        ) + 1
    metadata["context_metrics"] = metrics
    append = getattr(session, "append_event", None)
    if callable(append):
        append(ContextEventType.RUNTIME_ERROR, {
            "error_type": type(error).__name__,
            "message": str(error),
            "operation": "context_compaction",
        })


def _exception_chain(error: BaseException) -> list[BaseException]:
    result: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in result:
        result.append(current)
        current = current.__cause__ or current.__context__
    return result


def _group_closed(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        if str(message.get("role") or "") != "assistant" or not message.get("tool_calls"):
            continue
        expected = _tool_call_ids(message.get("tool_calls") or [])
        seen = {
            str(item.get("tool_call_id") or "")
            for item in messages
            if str(item.get("role") or "") == "tool"
        }
        if expected - seen:
            return False
    return True


def _tool_call_ids(calls: Sequence[Any]) -> set[str]:
    return {
        str(call.get("id"))
        for call in calls
        if isinstance(call, dict) and call.get("id")
    }


def _flatten(groups: Sequence[MessageGroup]) -> list[dict[str, Any]]:
    return [dict(message) for group in groups for message in group.messages]
