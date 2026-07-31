"""Optimistic, lifecycle-safe patches for the shared TaskStateCore."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping

from .models import (
    TERMINAL_TASK_STATUSES,
    TaskAction,
    TaskBlocker,
    TaskProgressItem,
    TaskStateCore,
    TaskStatus,
    utc_now,
)


class TaskStateValidationError(ValueError):
    pass


_STATUS_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.ACTIVE: {
        TaskStatus.BLOCKED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.BLOCKED: {
        TaskStatus.ACTIVE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}


@dataclass
class TaskStateCorePatch:
    base_version: int | None = None
    current_focus: str | None = None
    completed_add: list[TaskProgressItem] = field(default_factory=list)
    pending_replace: list[TaskAction] | None = None
    open_questions_replace: list[str] | None = None
    blockers_replace: list[TaskBlocker] | None = None
    completion_basis_add: list[str] = field(default_factory=list)
    requested_status: TaskStatus | None = None
    stop_reason: str | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> "TaskStateCorePatch":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            raise TaskStateValidationError("TaskStateCorePatch must be an object")
        data = dict(payload)
        requested = data.get("requested_status")
        try:
            requested_status = TaskStatus(str(requested)) if requested else None
        except ValueError as exc:
            raise TaskStateValidationError(f"unknown task status: {requested}") from exc
        base_version = data.get("base_version")
        return cls(
            base_version=int(base_version) if base_version is not None else None,
            current_focus=(
                str(data.get("current_focus") or "").strip()
                if "current_focus" in data
                else None
            ),
            completed_add=[
                _progress(item, index)
                for index, item in enumerate(data.get("completed_add") or [])
            ],
            pending_replace=(
                [_action(item, index) for index, item in enumerate(data.get("pending_replace") or [])]
                if "pending_replace" in data
                else None
            ),
            open_questions_replace=(
                [str(item).strip() for item in data.get("open_questions_replace") or [] if str(item).strip()]
                if "open_questions_replace" in data
                else None
            ),
            blockers_replace=(
                [_blocker(item, index) for index, item in enumerate(data.get("blockers_replace") or [])]
                if "blockers_replace" in data
                else None
            ),
            completion_basis_add=[
                str(item).strip() for item in data.get("completion_basis_add") or [] if str(item).strip()
            ],
            requested_status=requested_status,
            stop_reason=(
                str(data.get("stop_reason") or "").strip()
                if "stop_reason" in data
                else None
            ),
        )


def validate_task_state_core_patch(
    state: TaskStateCore,
    patch: TaskStateCorePatch,
) -> None:
    if patch.base_version is not None and patch.base_version != state.version:
        raise TaskStateValidationError(
            f"task state version conflict: expected {state.version}, got {patch.base_version}"
        )
    if state.status in TERMINAL_TASK_STATUSES and _patch_has_mutation(patch):
        raise TaskStateValidationError(
            f"terminal task state cannot be modified: {state.status}"
        )
    requested = patch.requested_status
    if requested is not None and requested != state.status:
        if requested not in _STATUS_TRANSITIONS[state.status]:
            raise TaskStateValidationError(
                f"illegal task status transition: {state.status} -> {requested}"
            )
    if requested == TaskStatus.COMPLETED:
        pending = patch.pending_replace if patch.pending_replace is not None else state.pending_actions
        blockers = patch.blockers_replace if patch.blockers_replace is not None else state.blockers
        basis = [*state.completion_basis, *patch.completion_basis_add]
        if pending:
            raise TaskStateValidationError("completed task state cannot retain pending actions")
        if blockers:
            raise TaskStateValidationError("completed task state cannot retain blockers")
        if not basis:
            raise TaskStateValidationError("completed task state requires completion_basis")


def apply_task_state_core_patch(
    state: TaskStateCore,
    patch: TaskStateCorePatch,
) -> TaskStateCore:
    validate_task_state_core_patch(state, patch)
    updated = deepcopy(state)
    if patch.current_focus is not None:
        updated.current_focus = patch.current_focus or None
    updated.completed.extend(deepcopy(patch.completed_add))
    if patch.pending_replace is not None:
        updated.pending_actions = deepcopy(patch.pending_replace)
    if patch.open_questions_replace is not None:
        updated.open_questions = list(patch.open_questions_replace)
    if patch.blockers_replace is not None:
        updated.blockers = deepcopy(patch.blockers_replace)
    updated.completion_basis = list(dict.fromkeys([
        *updated.completion_basis,
        *patch.completion_basis_add,
    ]))
    if patch.requested_status is not None:
        updated.status = patch.requested_status
    if patch.stop_reason is not None:
        updated.stop_reason = patch.stop_reason or None
    updated.version += 1
    updated.updated_at = utc_now()
    return updated


def _patch_has_mutation(patch: TaskStateCorePatch) -> bool:
    return bool(
        patch.current_focus is not None
        or patch.completed_add
        or patch.pending_replace is not None
        or patch.open_questions_replace is not None
        or patch.blockers_replace is not None
        or patch.completion_basis_add
        or patch.requested_status is not None
        or patch.stop_reason is not None
    )


def _progress(value: Any, index: int) -> TaskProgressItem:
    if isinstance(value, TaskProgressItem):
        return value
    data = dict(value) if isinstance(value, Mapping) else {"description": value}
    return TaskProgressItem(
        id=str(data.get("id") or f"progress:{index}"),
        description=str(data.get("description") or "").strip(),
        evidence_refs=[str(item) for item in data.get("evidence_refs") or [] if item],
    )


def _action(value: Any, index: int) -> TaskAction:
    if isinstance(value, TaskAction):
        return value
    data = dict(value) if isinstance(value, Mapping) else {"description": value}
    return TaskAction(
        id=str(data.get("id") or f"action:{index}"),
        description=str(data.get("description") or "").strip(),
        status=str(data.get("status") or "pending"),
        evidence_refs=[str(item) for item in data.get("evidence_refs") or [] if item],
    )


def _blocker(value: Any, index: int) -> TaskBlocker:
    if isinstance(value, TaskBlocker):
        return value
    data = dict(value) if isinstance(value, Mapping) else {"description": value}
    return TaskBlocker(
        id=str(data.get("id") or f"blocker:{index}"),
        description=str(data.get("description") or "").strip(),
        evidence_refs=[str(item) for item in data.get("evidence_refs") or [] if item],
        resolution_strategy=str(data.get("resolution_strategy") or "").strip(),
    )
