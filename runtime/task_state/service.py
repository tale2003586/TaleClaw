"""Session persistence and legacy migration for the shared task core."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any, Iterable, Mapping

from .legacy import (
    persist_task_state_migration_checkpoint,
    read_legacy_task_state,
    remove_legacy_task_state_keys,
)
from .models import (
    TASK_STATE_METADATA_KEY,
    TASK_STATE_SCHEMA,
    TaskStateCore,
    TaskStatus,
    payload_dict,
    task_state_envelope,
    utc_now,
)
from .patch import TaskStateCorePatch, apply_task_state_core_patch


class TaskStateRunObserver:
    """Project a generic run decision into optional durable task state."""

    def state_version(self, session: Any) -> int | None:
        state = load_task_state_core(session)
        return getattr(state, "version", None)

    def after_run(self, *, session: Any, execution: Any) -> None:
        decision = getattr(execution, "stop_decision", None)
        state = load_task_state_core(session)
        if decision is None or state is None:
            return

        from runtime.execution.failure_reasons import StopReason
        from .models import TERMINAL_TASK_STATUSES

        if state.status in TERMINAL_TASK_STATUSES:
            execution.stop_decision = replace(decision, task_state_version=state.version)
            return
        reason = str(decision.reason)
        if decision.reason is StopReason.COMPLETED:
            if state.pending_actions or state.blockers or not str(decision.message or "").strip():
                return
            patch = TaskStateCorePatch(
                base_version=state.version,
                current_focus="",
                completion_basis_add=[
                    "A final assistant response was produced with no pending actions or blockers."
                ],
                requested_status=TaskStatus.COMPLETED,
                stop_reason="assistant_final_message",
            )
        else:
            if decision.reason is StopReason.USER_CANCELLED:
                requested = TaskStatus.CANCELLED
            elif decision.reason in {
                StopReason.HARD_BUDGET_EXCEEDED,
                StopReason.NON_RETRYABLE_FAILURE,
            }:
                requested = TaskStatus.FAILED
            else:
                requested = TaskStatus.BLOCKED
            patch = TaskStateCorePatch(
                base_version=state.version,
                current_focus="",
                requested_status=requested,
                stop_reason=reason,
            )
        try:
            updated = apply_task_state_core_patch(state, patch)
        except ValueError:
            return
        save_task_state_core(session, updated)
        execution.stop_decision = replace(decision, task_state_version=updated.version)


def load_task_state_core(session: Any) -> TaskStateCore | None:
    """Load the newest valid core without importing a mode-specific application."""
    checkpoint_state: TaskStateCore | None = None
    for checkpoint in list(getattr(session, "checkpoints", []) or []):
        if not isinstance(checkpoint, Mapping):
            continue
        state = checkpoint.get("state")
        raw = state.get(TASK_STATE_METADATA_KEY) if isinstance(state, Mapping) else None
        restored = TaskStateCore.from_payload(raw)
        if restored is not None and (
            checkpoint_state is None or restored.version > checkpoint_state.version
        ):
            checkpoint_state = restored
    metadata = getattr(session, "metadata", {}) or {}
    metadata_state = TaskStateCore.from_payload(metadata.get(TASK_STATE_METADATA_KEY))
    if metadata_state is not None and (
        checkpoint_state is None or metadata_state.version >= checkpoint_state.version
    ):
        return metadata_state
    return checkpoint_state


def save_task_state_core(
    session: Any,
    state: TaskStateCore,
    *,
    extensions: Mapping[str, Any] | None = None,
) -> TaskStateCore:
    metadata = getattr(session, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
        session.metadata = metadata
    state.updated_at = utc_now()
    previous_payload = payload_dict(metadata.get(TASK_STATE_METADATA_KEY))
    if extensions is None:
        extensions = _preserved_extensions(metadata.get(TASK_STATE_METADATA_KEY))
    envelope = task_state_envelope(
        state,
        extensions=extensions,
    )
    previous_core = (
        previous_payload.get("core")
        if isinstance(previous_payload, dict)
        and previous_payload.get("schema") == TASK_STATE_SCHEMA
        else None
    )
    if isinstance(previous_core, Mapping) and isinstance(extensions.get("coding"), Mapping):
        envelope["core"] = _merge_coding_core_details(
            envelope["core"],
            previous_core,
        )
    metadata[TASK_STATE_METADATA_KEY] = envelope
    touch = getattr(session, "touch", None)
    if callable(touch):
        touch()
    return state


def ensure_task_state_core(
    session: Any,
    *,
    objective: str,
    artifact_refs: Iterable[str] = (),
    current_focus: str | None = "Understand the current request",
    checkpoint_persister=None,
) -> TaskStateCore:
    existing = load_task_state_core(session)
    refs = [str(item) for item in artifact_refs if str(item).strip()]
    current_task_id = _latest_user_task_id(session)
    if existing is not None:
        metadata = getattr(session, "metadata", {}) or {}
        persisted = payload_dict(metadata.get(TASK_STATE_METADATA_KEY))
        if persisted is not None and persisted.get("schema") != TASK_STATE_SCHEMA:
            save_task_state_core(
                session,
                existing,
                extensions={"coding_legacy": deepcopy(persisted)},
            )
        next_objective = _objective_text(objective)
        if existing.status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        } and current_task_id != existing.task_id:
            state = TaskStateCore(
                task_id=current_task_id,
                objective=next_objective,
                current_focus=current_focus,
                artifact_refs=list(dict.fromkeys(refs)),
            )
            save_task_state_core(session, state, extensions={})
            return state
        merged = list(dict.fromkeys([*existing.artifact_refs, *refs]))
        if merged != existing.artifact_refs:
            existing.artifact_refs = merged
            existing.version += 1
            save_task_state_core(session, existing)
        return existing

    metadata = getattr(session, "metadata", {}) or {}
    legacy = read_legacy_task_state(metadata)
    state = legacy.to_core() if legacy is not None else TaskStateCore(
        task_id=current_task_id,
        objective=_objective_text(objective),
        current_focus=current_focus,
    )
    state.artifact_refs = list(dict.fromkeys([*state.artifact_refs, *refs]))
    extensions: dict[str, Any] = {}
    raw_task_state = payload_dict(metadata.get(TASK_STATE_METADATA_KEY))
    if raw_task_state is not None and raw_task_state.get("schema") != TASK_STATE_SCHEMA:
        extensions["coding_legacy"] = deepcopy(raw_task_state)
    if legacy is None:
        save_task_state_core(session, state, extensions=extensions)
        return state

    previous = _migration_session_snapshot(session)
    try:
        save_task_state_core(session, state, extensions=extensions)
        remove_legacy_task_state_keys(session.metadata)
        persist_task_state_migration_checkpoint(
            session,
            task_state_payload=dict(session.metadata[TASK_STATE_METADATA_KEY]),
            source=legacy.source,
            source_payload=legacy.source_payload,
            checkpoint_persister=checkpoint_persister,
        )
    except Exception:
        _restore_migration_session_snapshot(session, previous)
        raise
    return state


def _preserved_extensions(payload: Any) -> dict[str, Any]:
    data = payload_dict(payload)
    if data is None:
        return {}
    if data.get("schema") == TASK_STATE_SCHEMA:
        extensions = data.get("extensions")
        return deepcopy(dict(extensions)) if isinstance(extensions, Mapping) else {}
    return {"coding_legacy": deepcopy(data)}


def _objective_text(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("summary")
    return str(value or "Answer the user's current request").strip()[:480]


def _latest_user_task_id(session: Any) -> str:
    session_id = str(getattr(session, "id", "") or "")
    for event in reversed(list(getattr(session, "event_log", []) or [])):
        if str(getattr(event, "type", "") or "") not in {"user_message", "user_correction"}:
            continue
        event_id = str(getattr(event, "event_id", "") or "")
        if event_id:
            return f"{session_id}:{event_id}"
    messages = list(getattr(session, "messages", []) or [])
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, Mapping) and str(message.get("role") or "") == "user":
            return f"{session_id}:message:{index}"
    return session_id


def _merge_coding_core_details(
    current: Mapping[str, Any],
    previous: Mapping[str, Any],
) -> dict[str, Any]:
    """Preserve coding-only detail on shared items untouched by a core patch."""
    merged = deepcopy(dict(current))
    previous_objective = previous.get("objective")
    if isinstance(previous_objective, Mapping) and isinstance(merged.get("objective"), str):
        merged["objective"] = {
            **deepcopy(dict(previous_objective)),
            "summary": merged["objective"],
        }
    for key in ("completed", "pending_actions", "blockers"):
        old_items = previous.get(key)
        new_items = merged.get(key)
        if not isinstance(old_items, list) or not isinstance(new_items, list):
            continue
        old_by_id = {
            str(item.get("id")): item
            for item in old_items
            if isinstance(item, Mapping) and item.get("id")
        }
        merged[key] = [
            {
                **deepcopy(dict(old_by_id.get(str(item.get("id")), {}))),
                **dict(item),
            }
            if isinstance(item, Mapping)
            else item
            for item in new_items
        ]
    return merged


def _migration_session_snapshot(session: Any) -> dict[str, Any]:
    return {
        "metadata": deepcopy(getattr(session, "metadata", {}) or {}),
        "event_log": list(getattr(session, "event_log", []) or []),
        "checkpoints": deepcopy(getattr(session, "checkpoints", []) or []),
        "archive_boundary_seq": getattr(session, "archive_boundary_seq", 0),
        "updated_at": getattr(session, "updated_at", None),
    }


def _restore_migration_session_snapshot(session: Any, snapshot: Mapping[str, Any]) -> None:
    session.metadata = snapshot["metadata"]
    session.event_log = snapshot["event_log"]
    session.checkpoints = snapshot["checkpoints"]
    session.archive_boundary_seq = snapshot["archive_boundary_seq"]
    if snapshot["updated_at"] is not None:
        session.updated_at = snapshot["updated_at"]
