"""Host-selected coding task identity and explicit user requirement revisions."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from typing import Any

from runtime.task_state.models import TaskStatus
from .task_state import (
    Constraint, ItemStatus, Objective, StateHistoryEntry, load_task_state, save_task_state,
)

ACTIVE_CODING_TASK_ID = "active_coding_task_id"
TASK_MODES = frozenset({"auto", "new", "continue", "branch"})


def coding_task_options(value: Any) -> dict[str, Any]:
    """Validate the explicit transport contract without inspecting message text."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("coding_task must be an object")
    unknown = set(value) - {"mode", "task_id", "requirements_revision", "context_policy", "workspace_mode", "base_ref"}
    if unknown:
        raise ValueError(f"Unknown coding_task field: {sorted(unknown)[0]}")
    mode = value.get("mode", "auto")
    if not isinstance(mode, str) or mode not in TASK_MODES:
        raise ValueError("coding_task.mode must be auto, new, continue, or branch")
    task_id = value.get("task_id")
    if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
        raise ValueError("coding_task.task_id must be a nonempty string")
    if mode == "new" and task_id:
        raise ValueError("A new task cannot specify an existing task_id")
    revision = validate_requirements_revision(value.get("requirements_revision"))
    if mode == "new" and revision:
        raise ValueError("A new task has no existing requirements to revise")
    result = {"task_mode": mode}
    workspace_mode = value.get("workspace_mode")
    if workspace_mode is not None:
        if workspace_mode not in ("worktree", "in_place"):
            raise ValueError("coding_task.workspace_mode must be worktree or in_place")
        result["workspace_mode"] = workspace_mode
    base_ref = value.get("base_ref")
    if base_ref is not None:
        if (not isinstance(base_ref, str) or not base_ref.strip() or len(base_ref) > 256
                or base_ref.startswith("-") or any(ord(c) < 32 for c in base_ref)):
            raise ValueError("coding_task.base_ref must be a nonempty Git commit reference")
        if mode == "continue" or workspace_mode == "in_place":
            raise ValueError("base_ref is only supported for a new or branched worktree")
        result["base_ref"] = base_ref
    if value.get("context_policy") is not None:
        # Reuse the runtime policy validator so the transport cannot silently
        # accept misspelled settings or choose an unsupported fallback.
        from types import SimpleNamespace
        from runtime.context.policies import context_policy
        policy_value = value["context_policy"]
        try:
            context_policy(SimpleNamespace(metadata={"context_policy": policy_value}))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid coding_task.context_policy: {exc}") from exc
        result["context_policy"] = deepcopy(policy_value)
    if task_id:
        result["task_id"] = task_id.strip()
    if revision:
        result["requirements_revision"] = revision
    return result


def validate_requirements_revision(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not value:
        raise ValueError("requirements_revision must be a nonempty object")
    if set(value) - {"objective", "constraints", "remove_constraint_ids"}:
        raise ValueError("Unknown requirements_revision field")
    result = deepcopy(value)
    if "objective" in result and (not isinstance(result["objective"], str) or not result["objective"].strip()):
        raise ValueError("requirements_revision.objective must be nonempty text")
    constraints = result.get("constraints", [])
    removals = result.get("remove_constraint_ids", [])
    if not isinstance(constraints, list) or not isinstance(removals, list):
        raise ValueError("Requirement constraints and removal ids must be lists")
    ids = []
    for item in constraints:
        if (not isinstance(item, dict) or set(item) - {"id", "text", "required"}
                or not isinstance(item.get("id"), str) or not item["id"].strip()
                or not isinstance(item.get("text"), str) or not item["text"].strip()
                or ("required" in item and not isinstance(item["required"], bool))):
            raise ValueError("Each constraint revision requires id and text, with optional boolean required")
        ids.append(item["id"])
    if not all(isinstance(item, str) and item.strip() for item in removals):
        raise ValueError("remove_constraint_ids must contain nonempty strings")
    if len(ids) != len(set(ids)) or len(removals) != len(set(removals)) or set(ids) & set(removals):
        raise ValueError("A constraint may be revised or removed only once per request")
    if not result.get("objective") and not constraints and not removals:
        raise ValueError("requirements_revision contains no changes")
    return result


def validate_revision_target(session, revision: dict[str, Any] | None) -> None:
    if not revision:
        return
    state = load_task_state(session)
    if state is None:
        raise ValueError("The task has no requirements to revise")
    known = {item.id for item in state.constraints}
    if any(item not in known for item in revision.get("remove_constraint_ids", [])):
        raise ValueError("Cannot remove an unknown constraint")


def apply_requirements_revision(session, revision: dict[str, Any], event_ref: str) -> None:
    """A host-authorized USER_CORRECTION changes facts, never an LLM summary."""
    validate_revision_target(session, revision)
    state = load_task_state(session)
    assert state is not None
    patch_id = f"user-revision:{event_ref}"
    if revision.get("objective"):
        previous = state.objective
        state.objective = Objective(
            summary=revision["objective"], original_request_ref=event_ref,
            supersedes=previous.original_request_ref,
        )
        state.history.append(StateHistoryEntry(
            id=f"{patch_id}:objective", category="objective", item_id="objective",
            previous=asdict(previous), replacement=asdict(state.objective), source_patch_id=patch_id,
        ))
    changes = {item["id"]: item for item in revision.get("constraints", [])}
    removals = set(revision.get("remove_constraint_ids", []))
    known = {item.id: item for item in state.constraints}
    for identifier in [*changes, *sorted(removals)]:
        previous = known.get(identifier)
        if identifier in removals:
            replacement = deepcopy(previous)
            replacement.required = False
            replacement.status = ItemStatus.SUPERSEDED
            replacement.superseded_by = event_ref
        else:
            change = changes[identifier]
            replacement = Constraint(
                id=identifier, text=change["text"], required=change.get("required", True),
                source_event_ref=event_ref,
            )
        state.history.append(StateHistoryEntry(
            id=f"{patch_id}:constraint:{identifier}", category="constraint", item_id=identifier,
            previous=asdict(previous) if previous is not None else {},
            replacement=asdict(replacement), source_patch_id=patch_id,
        ))
        if previous is None:
            state.constraints.append(replacement)
        else:
            state.constraints[state.constraints.index(previous)] = replacement
    _invalidate_revision_reasoning(state, "User requirements changed")
    state.version += 1
    state.status = TaskStatus.ACTIVE
    state.stop_reason = None
    save_task_state(session, state)
    session.metadata.setdefault("requirements_revision_events", []).append(event_ref)


def resume_task_state(session, *, branched: bool = False) -> None:
    state = load_task_state(session)
    if state is None:
        return
    changed = state.status != TaskStatus.ACTIVE or bool(state.stop_reason) or branched
    if not changed:
        return
    state.status = TaskStatus.ACTIVE
    state.stop_reason = None
    if branched:
        reason = "Inherited from a branched task; revalidate for this branch"
        for evidence in state.evidence_index.values():
            evidence.stale = True
            evidence.stale_reason = reason
        _invalidate_revision_reasoning(state, reason)
    state.version += 1
    save_task_state(session, state)


def _invalidate_revision_reasoning(state, reason: str) -> None:
    if state.completion_basis:
        state.history.append(StateHistoryEntry(
            id=f"host-validity:{state.version}:completion-basis", category="completion_basis",
            item_id="completion_basis", previous={"refs": list(state.completion_basis)},
            replacement={"refs": [], "reason": reason}, source_patch_id="host-task-lifecycle",
        ))
        state.completion_basis = []
    # Imported facts remain auditable; successful old execution is not proof
    # of satisfying a revised request or a new branch.
    for evidence in state.evidence_index.values():
        if getattr(evidence, "verification", False):
            evidence.stale = True
            evidence.stale_reason = reason
    for item in state.completed:
        item.needs_parent_verification = True
    for item in [*state.findings, *state.decisions, *state.plan]:
        if str(getattr(item, "status", "")) == "completed":
            item.status = ItemStatus.AWAITING_VERIFICATION
    from .reasoning_memory import invalidate_reasoning_for_revision
    invalidate_reasoning_for_revision(state, reason)
