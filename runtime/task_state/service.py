"""Session persistence and legacy migration for the shared task core."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Iterable, Mapping

from .models import (
    TASK_STATE_METADATA_KEY,
    TASK_STATE_SCHEMA,
    TaskAction,
    TaskProgressItem,
    TaskStateCore,
    TaskStatus,
    payload_dict,
    task_state_envelope,
    utc_now,
)


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
    metadata.pop("working_memory", None)
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
    migrated, source, source_payload = _migrate_legacy_core(metadata)
    state = migrated or TaskStateCore(
        task_id=current_task_id,
        objective=_objective_text(objective),
        current_focus=current_focus,
    )
    state.artifact_refs = list(dict.fromkeys([*state.artifact_refs, *refs]))
    extensions: dict[str, Any] = {}
    raw_task_state = payload_dict(metadata.get(TASK_STATE_METADATA_KEY))
    if raw_task_state is not None and raw_task_state.get("schema") != TASK_STATE_SCHEMA:
        extensions["coding_legacy"] = deepcopy(raw_task_state)
    if source:
        extensions["legacy_source"] = {
            "source": source,
            "payload": deepcopy(source_payload),
        }
    save_task_state_core(session, state, extensions=extensions)
    return state


def _migrate_legacy_core(
    metadata: Mapping[str, Any],
) -> tuple[TaskStateCore | None, str, dict[str, Any]]:
    working = _json_mapping(metadata.get("working_memory"))
    if working is not None:
        state = TaskStateCore(
            task_id=str(working.get("task_id") or ""),
            objective=_objective_text(working.get("objective")),
            status=(
                TaskStatus.COMPLETED
                if str(working.get("status") or "") == "completed"
                else TaskStatus.ACTIVE
            ),
            completed=[
                TaskProgressItem(
                    id=str(item.get("unit_id") or f"legacy-progress:{index}"),
                    description=str(item.get("conclusion") or item.get("description") or ""),
                    evidence_refs=[str(ref) for ref in item.get("evidence_refs") or []],
                )
                for index, item in enumerate(working.get("completed_units") or [])
                if isinstance(item, Mapping)
            ],
            pending_actions=[
                TaskAction(
                    id=str(item.get("unit_id") or f"legacy-action:{index}"),
                    description=str(item.get("description") or ""),
                    status=str(item.get("state") or item.get("status") or "pending"),
                )
                for index, item in enumerate(working.get("pending_units") or [])
                if isinstance(item, Mapping)
            ],
            current_focus="Resume migrated task progress",
            updated_at=str(working.get("updated_at") or "migration:unknown-time"),
        )
        return state, "working_memory", working
    coding_context = _json_mapping(metadata.get("coding_context_state"))
    if coding_context is not None and coding_context.get("objective"):
        state = TaskStateCore(
            task_id=str(coding_context.get("task_id") or ""),
            objective=_objective_text(coding_context.get("objective")),
            status=(
                TaskStatus.BLOCKED
                if str(coding_context.get("phase") or "") == "blocked"
                else TaskStatus.ACTIVE
            ),
            open_questions=[
                str(item.get("question") if isinstance(item, Mapping) else item)
                for item in coding_context.get("open_questions") or []
            ],
            current_focus="Resume migrated coding context",
            updated_at=str(coding_context.get("updated_at") or "migration:unknown-time"),
        )
        return state, "coding_context_state", coding_context
    return None, "", {}


def _preserved_extensions(payload: Any) -> dict[str, Any]:
    data = payload_dict(payload)
    if data is None:
        return {}
    if data.get("schema") == TASK_STATE_SCHEMA:
        extensions = data.get("extensions")
        return deepcopy(dict(extensions)) if isinstance(extensions, Mapping) else {}
    return {"coding_legacy": deepcopy(data)}


def _json_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return dict(value) if isinstance(value, Mapping) else None


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
