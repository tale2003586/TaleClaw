"""Validated explicit TaskState patches and deterministic event reduction."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Iterable, Sequence
from runtime.task_state.models import TERMINAL_TASK_STATUSES, TaskStatus
from runtime.task_state.patch import (
    TaskStateCorePatch,
    TaskStateValidationError,
    validate_task_state_core_patch,
)

from .task_state import (
    Action,
    Blocker,
    CompletedItem,
    Constraint,
    CoverageEntry,
    Decision,
    EvidenceRef,
    Finding,
    Hypothesis,
    ItemStatus,
    Objective,
    OpenQuestion,
    PlanItem,
    StateHistoryEntry,
    TaskPhase,
    TaskState,
)


class StateValidationError(TaskStateValidationError):
    pass


_PHASE_TRANSITIONS: dict[TaskPhase, set[TaskPhase]] = {
    TaskPhase.INTAKE: {TaskPhase.PLANNING, TaskPhase.BLOCKED},
    TaskPhase.PLANNING: {TaskPhase.EXPLORATION, TaskPhase.BLOCKED},
    TaskPhase.EXPLORATION: {TaskPhase.IMPLEMENTATION, TaskPhase.BLOCKED},
    TaskPhase.IMPLEMENTATION: {TaskPhase.VERIFICATION, TaskPhase.BLOCKED},
    TaskPhase.VERIFICATION: {TaskPhase.FINALIZATION, TaskPhase.BLOCKED},
    TaskPhase.FINALIZATION: {TaskPhase.BLOCKED},
    TaskPhase.BLOCKED: {
        TaskPhase.EXPLORATION,
        TaskPhase.IMPLEMENTATION,
        TaskPhase.VERIFICATION,
        TaskPhase.FINALIZATION,
    },
}

_ITEM_TRANSITIONS: dict[ItemStatus, set[ItemStatus]] = {
    ItemStatus.PENDING: {ItemStatus.IN_PROGRESS, ItemStatus.FAILED, ItemStatus.SUPERSEDED},
    ItemStatus.IN_PROGRESS: {
        ItemStatus.AWAITING_VERIFICATION,
        ItemStatus.FAILED,
        ItemStatus.SUPERSEDED,
    },
    ItemStatus.AWAITING_VERIFICATION: {
        ItemStatus.IN_PROGRESS,
        ItemStatus.COMPLETED,
        ItemStatus.FAILED,
        ItemStatus.SUPERSEDED,
    },
    ItemStatus.FAILED: {ItemStatus.PENDING, ItemStatus.IN_PROGRESS, ItemStatus.SUPERSEDED},
    ItemStatus.COMPLETED: {ItemStatus.SUPERSEDED},
    ItemStatus.SUPERSEDED: set(),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _patch_id(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return "patch:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _event_id(event: dict[str, Any]) -> str:
    return str(event.get("event_id") or event.get("id") or "")


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("event_type") or event.get("type") or "")


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    result = dict(payload) if isinstance(payload, dict) else dict(event)
    message = result.get("message")
    if isinstance(message, dict):
        return {**result, **message}
    return result


def _assign_patch_id(patch: "StatePatch") -> None:
    if patch.patch_id:
        return
    patch.patch_id = _patch_id({
        "origin": patch.origin,
        "source_event_ids": patch.source_event_ids,
        "phase": str(patch.phase or ""),
        "objective": _plain(patch.objective) if patch.objective else None,
        "ids": {
            "constraints": [item.id for item in patch.constraints],
            "plan": [item.id for item in patch.plan_items],
            "completed": [item.id for item in patch.completed],
            "findings": [item.id for item in patch.findings],
            "hypotheses": [item.id for item in patch.hypotheses],
            "decisions": [item.id for item in patch.decisions],
            "actions": [item.id for item in patch.pending_actions],
            "questions": [item.id for item in patch.open_questions],
            "blockers": [item.id for item in patch.blockers],
            "evidence": [item.id for item in patch.evidence],
            "coverage": [item.id for item in patch.coverage],
        },
    })


@dataclass
class ItemTransition:
    id: str
    status: ItemStatus


@dataclass
class StatePatch:
    """An additive proposal.  It intentionally has no `state` field."""

    patch_id: str = ""
    base_version: int | None = None
    source_event_ids: list[str] = field(default_factory=list)
    origin: str = "deterministic"
    requested_status: TaskStatus | None = None
    current_focus: str | None = None
    completion_basis_add: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    pending_replace: list[Action] | None = None
    open_questions_replace: list[OpenQuestion] | None = None
    blockers_replace: list[Blocker] | None = None
    phase: TaskPhase | None = None
    objective: Objective | None = None
    constraints: list[Constraint] = field(default_factory=list)
    plan_items: list[PlanItem] = field(default_factory=list)
    completed: list[CompletedItem] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    pending_actions: list[Action] = field(default_factory=list)
    open_questions: list[OpenQuestion] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    evidence: list[EvidenceRef] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    coverage: list[CoverageEntry] = field(default_factory=list)
    plan_transitions: list[ItemTransition] = field(default_factory=list)
    action_transitions: list[ItemTransition] = field(default_factory=list)
    question_transitions: list[ItemTransition] = field(default_factory=list)
    hypothesis_transitions: list[ItemTransition] = field(default_factory=list)
    files_inspected: list[str] = field(default_factory=list)
    symbols_inspected: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    tests_run: list[str] = field(default_factory=list)
    areas_unchecked: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # The id is assigned once all deterministic source events are known.
        # This prevents every empty constructor from sharing one accidental id.
        if self.phase is not None and not isinstance(self.phase, TaskPhase):
            self.phase = _coerce_phase(self.phase)
        if self.requested_status is not None and not isinstance(
            self.requested_status, TaskStatus
        ):
            self.requested_status = TaskStatus(str(self.requested_status))

    @classmethod
    def from_payload(cls, payload: Any) -> "StatePatch":
        if isinstance(payload, StatePatch):
            return payload
        if not isinstance(payload, dict):
            raise StateValidationError("semantic compactor must return a StatePatch or object")
        phase = payload.get("phase")
        requested_status = payload.get("requested_status")
        complete_actions = _completion_transitions(payload.get("complete_actions"))
        resolve_questions = _completion_transitions(payload.get("resolve_questions"))
        resolve_hypotheses = _completion_transitions(payload.get("resolve_hypotheses"))
        return cls(
            patch_id=str(payload.get("patch_id") or ""),
            base_version=(
                int(payload["base_version"])
                if payload.get("base_version") is not None
                else None
            ),
            source_event_ids=[str(value) for value in payload.get("source_event_ids") or []],
            origin=str(payload.get("origin") or "semantic"),
            requested_status=(
                TaskStatus(str(requested_status)) if requested_status else None
            ),
            current_focus=(
                str(payload.get("current_focus") or "").strip()
                if "current_focus" in payload
                else None
            ),
            completion_basis_add=[
                str(value) for value in payload.get("completion_basis_add") or [] if value
            ],
            stop_reason=(
                str(payload.get("stop_reason") or "").strip()
                if "stop_reason" in payload
                else None
            ),
            pending_replace=(
                _coerce_many(Action, payload.get("pending_replace"))
                if "pending_replace" in payload
                else None
            ),
            open_questions_replace=(
                _coerce_many(OpenQuestion, payload.get("open_questions_replace"))
                if "open_questions_replace" in payload
                else None
            ),
            blockers_replace=(
                _coerce_many(Blocker, payload.get("blockers_replace"))
                if "blockers_replace" in payload
                else None
            ),
            phase=_coerce_phase(phase) if phase else None,
            objective=_coerce(Objective, payload.get("objective")),
            constraints=_coerce_many(Constraint, payload.get("constraints") or payload.get("update_constraints")),
            plan_items=_coerce_many(PlanItem, payload.get("plan_items") or payload.get("plan") or payload.get("add_plan_items")),
            completed=_coerce_many(
                CompletedItem,
                payload.get("completed")
                or payload.get("add_completed")
                or payload.get("completed_add"),
            ),
            findings=_coerce_many(Finding, [
                *(payload.get("findings") or []),
                *(payload.get("add_findings") or []),
                *(payload.get("update_findings") or []),
            ]),
            hypotheses=_coerce_many(Hypothesis, payload.get("hypotheses") or payload.get("add_hypotheses")),
            decisions=_coerce_many(Decision, payload.get("decisions") or payload.get("add_decisions")),
            pending_actions=_coerce_many(Action, payload.get("pending_actions") or payload.get("add_pending_actions")),
            open_questions=_coerce_many(OpenQuestion, payload.get("open_questions") or payload.get("add_open_questions")),
            blockers=_coerce_many(
                Blocker,
                payload.get("blockers") or payload.get("add_blockers"),
            ),
            evidence=_coerce_many(
                EvidenceRef,
                payload.get("evidence")
                or payload.get("evidence_refs")
                or payload.get("add_evidence"),
            ),
            artifact_refs=[str(value) for value in payload.get("artifact_refs") or [] if value],
            coverage=_coerce_many(CoverageEntry, payload.get("coverage")),
            plan_transitions=_coerce_transitions(payload.get("plan_transitions")),
            action_transitions=[*_coerce_transitions(payload.get("action_transitions")), *complete_actions],
            question_transitions=[*_coerce_transitions(payload.get("question_transitions")), *resolve_questions],
            hypothesis_transitions=[*_coerce_transitions(payload.get("hypothesis_transitions")), *resolve_hypotheses],
            files_inspected=[str(value) for value in payload.get("files_inspected") or []],
            symbols_inspected=[str(value) for value in payload.get("symbols_inspected") or []],
            files_modified=[str(value) for value in payload.get("files_modified") or []],
            tests_run=[str(value) for value in payload.get("tests_run") or []],
            areas_unchecked=[str(value) for value in payload.get("areas_unchecked") or []],
        )


class DeterministicEventExtractor:
    """Maps typed raw events to facts without inferring claims from natural text."""

    def extract(self, events: Iterable[dict[str, Any]]) -> StatePatch:
        patch = StatePatch(origin="deterministic")
        calls_by_id: dict[str, dict[str, Any]] = {}
        for event in events:
            if not isinstance(event, dict) or not _event_id(event):
                continue
            event_id = _event_id(event)
            event_type = _event_type(event)
            payload = _payload(event)
            patch.source_event_ids.append(event_id)
            patch.artifact_refs.extend(_artifact_refs(payload, event))
            patch.evidence.extend(_structured_evidence(payload, event_id))
            patch.coverage.extend(_structured_coverage(payload))
            if event_type == "tool_result":
                call_id = str(payload.get("tool_call_id") or payload.get("call_id") or "")
                call = calls_by_id.get(call_id, {})
                enriched_payload = dict(payload)
                if call:
                    enriched_payload.setdefault("arguments", call.get("arguments") or {})
                    enriched_payload.setdefault("tool_name", call.get("name") or "")
                evidence = _tool_result_evidence(enriched_payload, event_id)
                patch.evidence.append(evidence)
                path = str(
                    enriched_payload.get("path")
                    or _tool_path(
                        enriched_payload.get("arguments")
                        or enriched_payload.get("final_arguments")
                    )
                    or ""
                )
                if path:
                    patch.coverage.append(CoverageEntry(
                        id=f"coverage:{event_id}", area=path,
                        evidence_refs=[evidence.id], summary=evidence.summary,
                    ))
                    tool_name = _tool_name(enriched_payload)
                    if tool_name in {"write_file", "edit_file", "apply_patch", "delete_file"}:
                        patch.files_modified.append(path)
                    else:
                        patch.files_inspected.append(path)
                if _looks_like_test_result(enriched_payload):
                    patch.tests_run.append(evidence.id)
            elif event_type == "tool_call":
                for call in _tool_calls(payload):
                    call_id = str(call.get("id") or "")
                    if call_id:
                        calls_by_id[call_id] = call
            elif event_type == "runtime_error":
                patch.blockers.append(Blocker(
                    id=f"blocker:{event_id}",
                    description=_summary(payload, 500) or "runtime error",
                ))
            elif event_type == "user_correction":
                summary = str(payload.get("objective_summary") or "").strip()
                if summary:
                    patch.objective = Objective(
                        summary=summary[:480], original_request_ref=event_id,
                        supersedes="current-objective",
                    )
            # Only explicitly structured payload fields become semantic facts.
            patch.plan_items.extend(_coerce_many(PlanItem, payload.get("plan_items")))
            patch.completed.extend(_coerce_many(CompletedItem, payload.get("completed")))
            patch.findings.extend(_coerce_many(Finding, payload.get("findings")))
            patch.hypotheses.extend(_coerce_many(Hypothesis, payload.get("hypotheses")))
            patch.decisions.extend(_coerce_many(Decision, payload.get("decisions")))
            patch.pending_actions.extend(_coerce_many(Action, payload.get("pending_actions")))
            patch.open_questions.extend(_coerce_many(OpenQuestion, payload.get("open_questions")))
        patch.artifact_refs = list(dict.fromkeys(patch.artifact_refs))
        return patch


def validate_state_patch(
    state: TaskState,
    patch: StatePatch,
) -> None:
    core_patch = TaskStateCorePatch(
        base_version=patch.base_version,
        current_focus=patch.current_focus,
        completed_add=list(patch.completed),
        pending_replace=(
            list(patch.pending_replace) if patch.pending_replace is not None else None
        ),
        open_questions_replace=(
            [item.question for item in patch.open_questions_replace]
            if patch.open_questions_replace is not None
            else None
        ),
        blockers_replace=(
            list(patch.blockers_replace) if patch.blockers_replace is not None else None
        ),
        completion_basis_add=list(patch.completion_basis_add),
        requested_status=patch.requested_status,
        stop_reason=patch.stop_reason,
    )
    try:
        validate_task_state_core_patch(state, core_patch)
    except TaskStateValidationError as exc:
        raise StateValidationError(str(exc)) from exc
    if state.status in TERMINAL_TASK_STATUSES and _coding_patch_has_changes(patch):
        raise StateValidationError(
            f"terminal task state cannot be modified: {state.status}"
        )
    _assign_patch_item_ids(patch)
    _assign_patch_id(patch)
    if patch.objective is not None:
        raise StateValidationError("TaskState patches cannot change the user objective")
    if patch.origin == "semantic":
        if patch.evidence:
            raise StateValidationError("semantic compaction cannot create EvidenceRef values")
        deterministic_fields = [
            patch.files_inspected,
            patch.symbols_inspected,
            patch.files_modified,
            patch.tests_run,
        ]
        if any(deterministic_fields):
            raise StateValidationError("semantic compaction cannot create execution or coverage facts")
        unknown_artifacts = [ref for ref in patch.artifact_refs if ref not in state.artifact_refs]
        if unknown_artifacts:
            raise StateValidationError(f"semantic compaction invented artifact refs: {unknown_artifacts}")
    if patch.phase is not None and patch.phase != state.phase:
        if patch.phase not in _PHASE_TRANSITIONS[state.phase]:
            raise StateValidationError(f"illegal phase transition: {state.phase} -> {patch.phase}")
    _validate_patch_ids(state, patch)
    available_evidence = set(state.evidence_index) | {entry.id for entry in patch.evidence}
    available_findings = {item.id for item in state.findings} | {item.id for item in patch.findings}
    for item in [*patch.findings, *patch.completed, *patch.decisions, *patch.coverage]:
        _validate_evidence_refs(item, available_evidence)
    for item in [*patch.plan_items, *patch.hypotheses, *patch.pending_actions,
                 *patch.open_questions, *patch.blockers]:
        _validate_evidence_refs(item, available_evidence)
    for decision in patch.decisions:
        unknown_findings = [ref for ref in decision.related_findings if ref not in available_findings]
        if unknown_findings:
            raise StateValidationError(f"Decision references unknown findings: {unknown_findings}")
    _validate_transitions(state.plan, patch.plan_transitions, "plan")
    _validate_transitions(state.pending_actions, patch.action_transitions, "action")
    _validate_transitions(state.open_questions, patch.question_transitions, "question")
    _validate_transitions(state.hypotheses, patch.hypothesis_transitions, "hypothesis")


def reduce_task_state(
    state: TaskState,
    patch: StatePatch,
) -> TaskState:
    validate_state_patch(state, patch)
    return _apply_patch(state, patch, validate=False)


def _assign_patch_item_ids(patch: StatePatch) -> None:
    groups = (
        ("constraint", patch.constraints),
        ("plan", patch.plan_items),
        ("completed", patch.completed),
        ("finding", patch.findings),
        ("hypothesis", patch.hypotheses),
        ("decision", patch.decisions),
        ("action", patch.pending_actions),
        ("question", patch.open_questions),
        ("blocker", patch.blockers),
        ("evidence", patch.evidence),
        ("coverage", patch.coverage),
    )
    for prefix, items in groups:
        for item in items:
            if str(getattr(item, "id", "") or ""):
                continue
            payload = _plain(item)
            payload.pop("id", None)
            digest = _patch_id({
                "origin": patch.origin,
                "source_event_ids": patch.source_event_ids,
                "category": prefix,
                "item": payload,
            }).removeprefix("patch:")
            item.id = f"{prefix}:{digest}"


def _apply_patch(state: TaskState, patch: StatePatch, *, validate: bool) -> TaskState:
    next_state = deepcopy(state)
    if patch.current_focus is not None:
        next_state.current_focus = patch.current_focus or None
    next_state.completion_basis = list(dict.fromkeys([
        *next_state.completion_basis,
        *patch.completion_basis_add,
    ]))
    if patch.requested_status is not None:
        next_state.status = patch.requested_status
    if patch.stop_reason is not None:
        next_state.stop_reason = patch.stop_reason or None
    if patch.pending_replace is not None:
        next_state.pending_actions = deepcopy(patch.pending_replace)
    if patch.open_questions_replace is not None:
        next_state.open_questions = deepcopy(patch.open_questions_replace)
    if patch.blockers_replace is not None:
        next_state.blockers = deepcopy(patch.blockers_replace)
    if patch.objective is not None:
        _record_history(next_state, "objective", "objective", next_state.objective, patch.objective, patch.patch_id)
        next_state.objective = deepcopy(patch.objective)
    if patch.phase is not None:
        next_state.phase = patch.phase
    _upsert_many(next_state, "constraints", patch.constraints, patch.patch_id)
    _upsert_many(next_state, "plan", patch.plan_items, patch.patch_id)
    _upsert_many(next_state, "completed", patch.completed, patch.patch_id)
    _upsert_many(next_state, "findings", patch.findings, patch.patch_id)
    _upsert_many(next_state, "hypotheses", patch.hypotheses, patch.patch_id)
    _upsert_many(next_state, "decisions", patch.decisions, patch.patch_id)
    _upsert_many(next_state, "pending_actions", patch.pending_actions, patch.patch_id)
    _upsert_many(next_state, "open_questions", patch.open_questions, patch.patch_id)
    _upsert_many(next_state, "blockers", patch.blockers, patch.patch_id)
    _upsert_many(next_state.coverage, "entries", patch.coverage, patch.patch_id, state_owner=next_state)
    for evidence in patch.evidence:
        previous = next_state.evidence_index.get(evidence.id)
        if previous is not None and previous != evidence:
            _record_history(next_state, "evidence", evidence.id, previous, evidence, patch.patch_id)
        next_state.evidence_index[evidence.id] = deepcopy(evidence)
    next_state.artifact_refs = list(dict.fromkeys([*next_state.artifact_refs, *patch.artifact_refs]))
    coverage = next_state.coverage
    coverage.files_inspected = list(dict.fromkeys([*coverage.files_inspected, *patch.files_inspected]))
    coverage.symbols_inspected = list(dict.fromkeys([*coverage.symbols_inspected, *patch.symbols_inspected]))
    coverage.files_modified = list(dict.fromkeys([*coverage.files_modified, *patch.files_modified]))
    coverage.tests_run = list(dict.fromkeys([*coverage.tests_run, *patch.tests_run]))
    coverage.areas_unchecked = list(dict.fromkeys([*coverage.areas_unchecked, *patch.areas_unchecked]))
    _apply_transitions(next_state.plan, patch.plan_transitions)
    _apply_transitions(next_state.pending_actions, patch.action_transitions)
    _apply_transitions(next_state.open_questions, patch.question_transitions)
    _apply_transitions(next_state.hypotheses, patch.hypothesis_transitions)
    next_state.version += 1
    next_state.updated_at = _now()
    return next_state


def _coding_patch_has_changes(patch: StatePatch) -> bool:
    return bool(
        patch.objective is not None
        or patch.phase is not None
        or patch.constraints
        or patch.plan_items
        or patch.completed
        or patch.findings
        or patch.hypotheses
        or patch.decisions
        or patch.pending_actions
        or patch.open_questions
        or patch.blockers
        or patch.evidence
        or patch.artifact_refs
        or patch.coverage
        or patch.plan_transitions
        or patch.action_transitions
        or patch.question_transitions
        or patch.hypothesis_transitions
        or patch.files_inspected
        or patch.symbols_inspected
        or patch.files_modified
        or patch.tests_run
        or patch.areas_unchecked
    )


def _validate_patch_ids(state: TaskState, patch: StatePatch) -> None:
    categories = {
        "constraints": (state.constraints, patch.constraints),
        "plan": (state.plan, patch.plan_items),
        "completed": (state.completed, patch.completed),
        "findings": (state.findings, patch.findings),
        "hypotheses": (state.hypotheses, patch.hypotheses),
        "decisions": (state.decisions, patch.decisions),
        "pending_actions": (state.pending_actions, patch.pending_actions),
        "open_questions": (state.open_questions, patch.open_questions),
        "blockers": (state.blockers, patch.blockers),
        "evidence": (list(state.evidence_index.values()), patch.evidence),
        "coverage": (state.coverage.entries, patch.coverage),
    }
    for category, (existing, additions) in categories.items():
        seen: set[str] = set()
        existing_ids = {item.id for item in existing}
        for item in additions:
            if not item.id:
                raise StateValidationError(f"{category} item is missing a stable id")
            if item.id in seen:
                raise StateValidationError(f"duplicate {category} id in patch: {item.id}")
            seen.add(item.id)
            # Existing IDs are accepted only for explicit replacement/superseding.
            if item.id in existing_ids and not getattr(item, "supersedes", ""):
                existing_item = next(value for value in existing if value.id == item.id)
                if existing_item != item:
                    raise StateValidationError(f"duplicate {category} id: {item.id}")


def _validate_evidence_refs(item: Any, available: set[str]) -> None:
    refs = getattr(item, "evidence_refs", []) or []
    unknown = [str(ref) for ref in refs if str(ref) not in available]
    if unknown:
        raise StateValidationError(f"{type(item).__name__} references unknown evidence: {unknown}")
    if isinstance(item, Finding) and not refs:
        raise StateValidationError("Finding requires at least one EvidenceRef")


def _validate_transitions(items: list[Any], transitions: list[ItemTransition], category: str) -> None:
    by_id = {item.id: item for item in items}
    seen: set[str] = set()
    for transition in transitions:
        if transition.id in seen:
            raise StateValidationError(f"duplicate {category} transition: {transition.id}")
        seen.add(transition.id)
        item = by_id.get(transition.id)
        if item is None:
            raise StateValidationError(f"unknown {category} transition target: {transition.id}")
        current = getattr(item, "status", ItemStatus.PENDING)
        if transition.status != current and transition.status not in _ITEM_TRANSITIONS[current]:
            raise StateValidationError(f"illegal {category} transition: {current} -> {transition.status}")


def _upsert_many(owner: Any, attribute: str, entries: list[Any], patch_id: str, *, state_owner: TaskState | None = None) -> None:
    items = getattr(owner, attribute)
    state = state_owner or owner
    for entry in entries:
        for index, previous in enumerate(items):
            if previous.id == entry.id:
                if previous != entry:
                    _record_history(state, attribute, entry.id, previous, entry, patch_id)
                    items[index] = deepcopy(entry)
                break
        else:
            items.append(deepcopy(entry))


def _record_history(state: TaskState, category: str, item_id: str, previous: Any, replacement: Any, patch_id: str) -> None:
    state.history.append(StateHistoryEntry(
        id=f"history:{patch_id}:{category}:{item_id}", category=category, item_id=item_id,
        previous=_plain(previous), replacement=_plain(replacement), source_patch_id=patch_id,
    ))


def _apply_transitions(items: list[Any], transitions: list[ItemTransition]) -> None:
    by_id = {item.id: item for item in items}
    for transition in transitions:
        by_id[transition.id].status = transition.status


def _closed_event_prefix(events: Sequence[dict[str, Any]], boundary: str | None) -> list[dict[str, Any]]:
    ordered = [dict(event) for event in events if isinstance(event, dict) and _event_id(event)]
    if boundary:
        for index, event in enumerate(ordered):
            if _event_id(event) == boundary:
                ordered = ordered[index + 1:]
                break
    open_calls: set[str] = set()
    safe_end = -1
    for index, event in enumerate(ordered):
        event_type, payload = _event_type(event), _payload(event)
        if event_type == "tool_result":
            call_id = str(payload.get("tool_call_id") or payload.get("call_id") or "")
            if call_id:
                open_calls.discard(call_id)
        elif event_type == "tool_call":
            for call in _tool_calls(payload):
                call_id = str(call.get("id") or "")
                if call_id:
                    open_calls.add(call_id)
        else:
            for call in payload.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id"):
                    open_calls.add(str(call["id"]))
        if not open_calls:
            safe_end = index
    return ordered[:safe_end + 1]


def _structured_evidence(payload: dict[str, Any], event_id: str) -> list[EvidenceRef]:
    result = []
    for item in payload.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("id") or f"evidence:{event_id}:{len(result)}")
        result.append(EvidenceRef(
            id=evidence_id, event_id=str(item.get("event_id") or event_id),
            kind=str(item.get("kind") or "event"), summary=_summary(item, 500),
            artifact_ref=str(item.get("artifact_ref") or ""), tool_result_ref=str(item.get("tool_result_ref") or ""),
            path=str(item.get("path") or ""), lines=str(item.get("lines") or ""),
        ))
    return result


def _structured_coverage(payload: dict[str, Any]) -> list[CoverageEntry]:
    return _coerce_many(CoverageEntry, payload.get("coverage"))


def _tool_result_evidence(payload: dict[str, Any], event_id: str) -> EvidenceRef:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    artifact_ref = _ref_uri(payload.get("artifact_ref") or metadata.get("artifact_ref"))
    tool_result_ref = _ref_uri(payload.get("tool_result_ref") or metadata.get("tool_result_ref"))
    content = str(
        payload.get("summary")
        or payload.get("content_summary")
        or payload.get("output_preview")
        or payload.get("content")
        or ""
    )
    return EvidenceRef(
        id=f"evidence:{event_id}", event_id=event_id, kind="tool_result",
        summary=content[:500], artifact_ref=artifact_ref,
        tool_result_ref=tool_result_ref,
        path=str(payload.get("path") or _tool_path(payload.get("arguments") or payload.get("final_arguments")) or ""),
        content_hash=str(payload.get("content_hash") or payload.get("sha256") or metadata.get("sha256") or ""),
        uri=artifact_ref or tool_result_ref or f"event://{event_id}",
    )


def _artifact_refs(payload: dict[str, Any], event: dict[str, Any]) -> list[str]:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    refs = payload.get("artifact_refs") or event.get("artifact_refs") or []
    singular = payload.get("artifact_ref") or metadata.get("artifact_ref")
    if singular:
        if isinstance(singular, dict):
            singular = singular.get("storage_uri") or singular.get("artifact_id")
        refs = [*([refs] if isinstance(refs, str) else refs), singular]
    if isinstance(refs, str):
        refs = [refs]
    return [_ref_uri(value) for value in refs if _ref_uri(value)]


def _ref_uri(value: Any) -> str:
    if isinstance(value, dict):
        return str(
            value.get("storage_uri")
            or value.get("uri")
            or value.get("artifact_id")
            or value.get("result_id")
            or ""
        )
    return str(value or "")


def _tool_calls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    calls = payload.get("tool_calls")
    if isinstance(calls, list):
        result = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            arguments = function.get("arguments") or call.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"raw": arguments}
            result.append({
                "id": str(call.get("id") or ""),
                "name": str(function.get("name") or call.get("name") or ""),
                "arguments": arguments if isinstance(arguments, dict) else {},
            })
        return result
    return [{
        "id": str(payload.get("tool_call_id") or payload.get("call_id") or ""),
        "name": _tool_name(payload),
        "arguments": payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {},
    }]


def _tool_name(payload: dict[str, Any]) -> str:
    return str(payload.get("tool_name") or payload.get("name") or "")


def _looks_like_test_result(payload: dict[str, Any]) -> bool:
    tool_name = _tool_name(payload).lower()
    text = " ".join(str(payload.get(key) or "") for key in ("summary", "content", "output_preview"))
    return (
        "test" in tool_name
        or "pytest" in text.lower()
        or "tests passed" in text.lower()
    )


def _tool_path(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return ""
    return str(arguments.get("path") or arguments.get("file") or "")


def _summary(value: Any, limit: int) -> str:
    if isinstance(value, dict):
        value = value.get("summary") or value.get("message") or value.get("error") or ""
    return str(value or "").strip()[:limit]


def _coerce_phase(value: Any) -> TaskPhase:
    try:
        return TaskPhase(str(value))
    except ValueError as exc:
        raise StateValidationError(f"unknown task phase: {value}") from exc


def _coerce_status(value: Any) -> ItemStatus:
    value = "pending" if value in (None, "todo") else str(value)
    try:
        return ItemStatus(value)
    except ValueError as exc:
        raise StateValidationError(f"unknown item status: {value}") from exc


def _coerce(type_: type, value: Any) -> Any | None:
    if value is None:
        return None
    if isinstance(value, type_):
        return value
    if not isinstance(value, dict):
        return None
    source = dict(value)
    if type_ is Decision and "choice" not in source and "summary" in source:
        source["choice"] = source.get("summary")
    fields = type_.__dataclass_fields__
    data = {key: item for key, item in source.items() if key in fields}
    if "id" in fields:
        data.setdefault("id", "")
    if type_ is Objective:
        data.setdefault("summary", "")
    if "status" in data:
        data["status"] = _coerce_status(data["status"])
    if "evidence_refs" in data:
        data["evidence_refs"] = _refs(data["evidence_refs"])
    return type_(**data)


def _coerce_many(type_: type, values: Any) -> list[Any]:
    result = []
    for value in values or []:
        converted = _coerce(type_, value)
        if converted is not None:
            result.append(converted)
    return result


def _coerce_transitions(values: Any) -> list[ItemTransition]:
    result = []
    for value in values or []:
        if isinstance(value, ItemTransition):
            result.append(value)
        elif isinstance(value, dict) and value.get("id"):
            result.append(ItemTransition(str(value["id"]), _coerce_status(value.get("status"))))
    return result


def _completion_transitions(values: Any) -> list[ItemTransition]:
    result: list[ItemTransition] = []
    for value in values or []:
        if isinstance(value, str):
            result.append(ItemTransition(value, ItemStatus.COMPLETED))
        elif isinstance(value, dict) and value.get("id"):
            result.append(ItemTransition(str(value["id"]), ItemStatus.COMPLETED))
    return result


def _semantic_event_view(event: dict[str, Any]) -> dict[str, Any]:
    payload = _payload(event)
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    artifact = payload.get("artifact_ref") or metadata.get("artifact_ref")
    content = str(payload.get("content") or payload.get("summary") or "")
    return {
        "event_id": _event_id(event),
        "event_type": _event_type(event),
        "content": content[:1200],
        "tool_calls": _tool_calls(payload) if _event_type(event) == "tool_call" else [],
        "tool_call_id": str(payload.get("tool_call_id") or payload.get("call_id") or ""),
        "status": str(payload.get("status") or ""),
        "artifact_ref": artifact,
    }


def _refs(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value if item] if isinstance(value, list) else []


def _plain(value: Any) -> dict[str, Any]:
    if hasattr(value, "__dataclass_fields__"):
        return {key: _plain(item) if hasattr(item, "__dataclass_fields__") else item.value if isinstance(item, (TaskPhase, ItemStatus)) else item for key, item in value.__dict__.items()}
    return dict(value) if isinstance(value, dict) else {"value": str(value)}
