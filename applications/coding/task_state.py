"""Authoritative, serialisable state for a CodingApplication task.

This module intentionally has no Session dependency.  The Session/event adapters
own persistence; this object owns the one mutable description of task progress.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from typing import Any, Callable, Iterable

from runtime.task_state.models import (
    TASK_STATE_INITIAL_VERSION,
    TASK_STATE_METADATA_KEY,
    TASK_STATE_SCHEMA,
    TaskStateCore,
    TaskStatus,
    task_state_envelope,
)


TASK_STATE_VERSION = TASK_STATE_INITIAL_VERSION
OBJECTIVE_SUMMARY_LIMIT = 480


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return f"{prefix}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]}"


def _text(value: Any, limit: int = 800) -> str:
    return str(value or "").strip()[:limit]


class TaskPhase(StrEnum):
    INTAKE = "intake"
    PLANNING = "planning"
    EXPLORATION = "exploration"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    FINALIZATION = "finalization"
    BLOCKED = "blocked"
    COMPLETED = "finalization"  # legacy alias; completion is an item status


class ItemStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    AWAITING_VERIFICATION = "awaiting_verification"
    COMPLETED = "completed"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    BLOCKED = "failed"  # legacy alias
    CANCELLED = "superseded"  # legacy alias


@dataclass
class Objective:
    summary: str
    original_request_ref: str = ""
    source_artifacts: list[str] = field(default_factory=list)
    finish_condition: str = ""
    supersedes: str = ""


@dataclass
class Constraint:
    id: str
    text: str
    source_event_ref: str = ""
    required: bool = True
    status: ItemStatus = ItemStatus.PENDING
    superseded_by: str = ""


@dataclass
class EvidenceRef:
    id: str
    event_id: str
    kind: str = "event"
    summary: str = ""
    artifact_ref: str = ""
    tool_result_ref: str = ""
    path: str = ""
    lines: str = ""
    content_hash: str = ""
    uri: str = ""


@dataclass
class PlanItem:
    id: str
    description: str
    status: ItemStatus = ItemStatus.PENDING
    evidence_refs: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    supersedes: str = ""


@dataclass
class CompletedItem:
    id: str
    description: str
    evidence_refs: list[str] = field(default_factory=list)
    source_event_ids: list[str] = field(default_factory=list)
    supersedes: str = ""
    covered_scope: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    needs_parent_verification: bool = False


@dataclass
class Finding:
    id: str
    claim: str
    evidence_refs: list[str]
    confidence: str = "medium"
    supersedes: str = ""
    superseded_by: str = ""
    status: ItemStatus = ItemStatus.COMPLETED


@dataclass
class Hypothesis:
    id: str
    claim: str
    rationale: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    status: ItemStatus = ItemStatus.PENDING
    supersedes: str = ""


@dataclass
class Decision:
    id: str
    choice: str
    rationale: str = ""
    alternatives_rejected: list[str] = field(default_factory=list)
    related_findings: list[str] = field(default_factory=list)
    status: ItemStatus = ItemStatus.COMPLETED
    evidence_refs: list[str] = field(default_factory=list)
    supersedes: str = ""

    @property
    def summary(self) -> str:
        return self.choice


@dataclass
class Action:
    id: str
    description: str
    status: ItemStatus = ItemStatus.PENDING
    evidence_refs: list[str] = field(default_factory=list)
    priority: str = "P1"
    supersedes: str = ""
    observed_effects: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    scope_files: list[str] = field(default_factory=list)


@dataclass
class OpenQuestion:
    id: str
    question: str
    status: ItemStatus = ItemStatus.PENDING
    evidence_refs: list[str] = field(default_factory=list)
    supersedes: str = ""
    reason: str = ""
    resolution_strategy: str = ""
    resolved_by: str = ""


@dataclass
class Blocker:
    id: str
    description: str
    evidence_refs: list[str] = field(default_factory=list)
    status: ItemStatus = ItemStatus.BLOCKED
    supersedes: str = ""
    source_event_ref: str = ""
    resolution_strategy: str = ""
    resolved_by: str = ""


@dataclass
class CoverageEntry:
    id: str
    area: str
    status: str = "observed"
    evidence_refs: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class CoverageState:
    entries: list[CoverageEntry] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    files_inspected: list[str] = field(default_factory=list)
    symbols_inspected: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    tests_run: list[str] = field(default_factory=list)
    sections_inspected: list[str] = field(default_factory=list)
    ranges_read: list[str] = field(default_factory=list)
    samples_checked: list[str] = field(default_factory=list)
    areas_unchecked: list[str] = field(default_factory=list)


@dataclass
class StateHistoryEntry:
    id: str
    category: str
    item_id: str
    previous: dict[str, Any]
    replacement: dict[str, Any]
    source_patch_id: str = ""
    created_at: str = field(default_factory=_now)


@dataclass
class TaskState(TaskStateCore):
    """Coding extension of the shared runtime TaskStateCore.

    The inherited lifecycle fields remain the only mutable authority.  Richer
    coding item types refine the common collections for compatibility with the
    existing compactor and checkpoint format.
    """

    objective: Objective = field(default_factory=lambda: Objective(""))
    constraints: list[Constraint] = field(default_factory=list)
    completed: list[CompletedItem] = field(default_factory=list)
    pending_actions: list[Action] = field(default_factory=list)
    open_questions: list[OpenQuestion] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    phase: TaskPhase = TaskPhase.INTAKE
    plan: list[PlanItem] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    evidence_index: dict[str, EvidenceRef] = field(default_factory=dict)
    coverage: CoverageState = field(default_factory=CoverageState)
    history: list[StateHistoryEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return task_state_envelope(
            self,
            extensions={"coding": self.coding_extension_dict()},
        )

    def core_dict(self) -> dict[str, Any]:
        return _to_plain({
            "task_id": self.task_id,
            "version": self.version,
            "objective": self.objective,
            "constraints": self.constraints,
            "status": self.status,
            "current_focus": self.current_focus,
            "completed": self.completed,
            "pending_actions": self.pending_actions,
            "open_questions": self.open_questions,
            "blockers": self.blockers,
            "completion_basis": self.completion_basis,
            "stop_reason": self.stop_reason,
            "artifact_refs": self.artifact_refs,
            "updated_at": self.updated_at,
        })

    def coding_extension_dict(self) -> dict[str, Any]:
        return _to_plain({
            "phase": self.phase,
            "plan": self.plan,
            "findings": self.findings,
            "hypotheses": self.hypotheses,
            "decisions": self.decisions,
            "evidence_index": self.evidence_index,
            "coverage": self.coverage,
            "history": self.history,
        })

    @classmethod
    def from_payload(cls, payload: Any) -> "TaskState | None":
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schema") == TASK_STATE_SCHEMA:
            core_data = payload.get("core")
            extensions = payload.get("extensions")
            if not isinstance(core_data, dict):
                return None
            extensions = extensions if isinstance(extensions, dict) else {}
            coding_data = extensions.get("coding")
            if not isinstance(coding_data, dict):
                legacy = extensions.get("coding_legacy")
                if isinstance(legacy, dict):
                    restored = cls.from_payload(legacy)
                    if restored is not None:
                        _merge_shared_core_payload(restored, core_data)
                    return restored
                legacy_source = extensions.get("legacy_source")
                if isinstance(legacy_source, dict):
                    source = str(legacy_source.get("source") or "")
                    migrated = migrate_legacy_task_state(
                        legacy_source.get("payload"),
                        source=source,
                    ) if source in {"working_memory", "coding_context_state"} else None
                    if migrated is not None:
                        _merge_shared_core_payload(migrated, core_data)
                    return migrated
                coding_data = {}
            payload = {**core_data, **coding_data}
        objective_data = payload.get("objective")
        if isinstance(objective_data, str):
            objective_data = {"summary": objective_data}
        if not isinstance(objective_data, dict):
            return None
        try:
            return cls(
                objective=_coerce(Objective, objective_data),
                task_id=str(payload.get("task_id") or ""),
                constraints=_coerce_constraints(payload.get("constraints")),
                status=_task_status(payload.get("status"), phase=payload.get("phase")),
                current_focus=_optional_text(payload.get("current_focus")),
                phase=_phase(payload.get("phase")),
                plan=_coerce_list(PlanItem, payload.get("plan")),
                completed=_coerce_list(CompletedItem, payload.get("completed")),
                findings=_coerce_list(Finding, payload.get("findings")),
                hypotheses=_coerce_list(Hypothesis, payload.get("hypotheses")),
                decisions=_coerce_list(Decision, payload.get("decisions")),
                pending_actions=_coerce_list(Action, payload.get("pending_actions")),
                open_questions=_coerce_questions(payload.get("open_questions")),
                blockers=_coerce_list(Blocker, payload.get("blockers")),
                completion_basis=[
                    str(item) for item in payload.get("completion_basis") or [] if item
                ],
                stop_reason=_optional_text(payload.get("stop_reason")),
                evidence_index={
                    str(key): _coerce(EvidenceRef, value)
                    for key, value in _mapping(payload.get("evidence_index")).items()
                    if isinstance(value, dict)
                },
                artifact_refs=[str(item) for item in payload.get("artifact_refs") or [] if item],
                coverage=_coerce(CoverageState, payload.get("coverage") or {}),
                history=_coerce_list(StateHistoryEntry, payload.get("history")),
                version=max(1, int(payload.get("version") or TASK_STATE_VERSION)),
                updated_at=str(payload.get("updated_at") or _now()),
            )
        except (TypeError, ValueError):
            return None


def migrate_working_memory_payload(
    payload: Any,
    *,
    original_request_ref: str = "",
) -> TaskState | None:
    """Create a state snapshot from the legacy WorkingMemory representation.

    It deliberately stores a shortened objective and retains only event/tool refs;
    the original request itself remains in the event log or artifact store.
    """
    data = _payload_dict(payload)
    if data is None:
        return None
    objective = Objective(
        summary=_text(data.get("objective"), OBJECTIVE_SUMMARY_LIMIT),
        original_request_ref=original_request_ref,
    )
    state = TaskState(objective=objective)
    # Migration must be repeatable even when legacy records lack timestamps.
    state.updated_at = str(data.get("updated_at") or "migration:unknown-time")
    for item in data.get("completed_units") or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("unit_id") or _stable_id("completed", item))
        state.completed.append(CompletedItem(
            id=item_id,
            description=_text(item.get("conclusion") or item.get("description")),
            evidence_refs=[str(ref) for ref in item.get("evidence_refs") or []],
        ))
    for item in data.get("pending_units") or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("unit_id") or _stable_id("action", item))
        state.pending_actions.append(Action(
            id=item_id,
            description=_text(item.get("description")),
            status=_status(item.get("state") or item.get("status")),
            priority=_text(item.get("priority"), 8) or "P1",
        ))
    # Legacy execution traces intentionally stay in the event log.  Migration
    # only promotes durable task semantics into TaskState.
    findings = data.get("archived_findings") or {}
    if isinstance(findings, dict):
        for key, value in findings.items():
            if key in {"last_reasoning_step", "last_stop", "final_answer"}:
                continue
            state.hypotheses.append(Hypothesis(
                id=f"legacy-hypothesis:{key}",
                claim=_text(value, 500),
                rationale="migrated legacy finding without an EvidenceRef",
            ))
    state.phase = TaskPhase.FINALIZATION if data.get("status") == "completed" else TaskPhase.EXPLORATION
    return state


def migrate_coding_context_state_payload(
    payload: Any,
    *,
    original_request_ref: str = "",
) -> TaskState | None:
    """Migrate old CodingContextState data without promoting unproven claims."""
    data = _payload_dict(payload)
    if data is None:
        return None
    state = TaskState(objective=Objective(
        summary=_text(data.get("objective"), OBJECTIVE_SUMMARY_LIMIT),
        original_request_ref=original_request_ref,
        finish_condition=_text(data.get("finish_condition"), 480),
    ), phase=_phase(data.get("phase")))
    state.updated_at = str(data.get("updated_at") or "migration:unknown-time")
    evidence = data.get("evidence_index") or {}
    if isinstance(evidence, dict):
        for key, item in evidence.items():
            if not isinstance(item, dict):
                continue
            state.evidence_index[str(key)] = EvidenceRef(
                id=str(key), event_id=str(item.get("event_id") or item.get("source_event_id") or ""),
                kind=_text(item.get("kind"), 64) or "legacy",
                summary=_text(item.get("summary"), 500),
                artifact_ref=_text(item.get("artifact_ref") or item.get("tool_result"), 500),
                path=_text(item.get("path"), 500), lines=_text(item.get("lines"), 64),
            )
    for item in data.get("findings") or []:
        if not isinstance(item, dict):
            continue
        refs = _refs(item.get("evidence_refs") or item.get("evidence"))
        claim = _text(item.get("claim") or item.get("summary"), 600)
        item_id = str(item.get("id") or _stable_id("legacy-finding", item))
        if refs and all(ref in state.evidence_index for ref in refs):
            state.findings.append(Finding(item_id, claim, refs))
        else:
            state.hypotheses.append(Hypothesis(
                item_id, claim, "migrated unsupported CodingContextState finding", refs
            ))
    for item in data.get("pending_actions") or []:
        if isinstance(item, dict):
            state.pending_actions.append(Action(
                id=str(item.get("id") or _stable_id("legacy-action", item)),
                description=_text(item.get("description") or item.get("summary")),
                status=_status(item.get("status")),
            ))
    state.open_questions = [
        OpenQuestion(f"legacy-question:{index}", _text(value, 600))
        for index, value in enumerate(data.get("open_questions") or [])
        if _text(value, 600)
    ]
    state.coverage = CoverageState(entries=[
        CoverageEntry(
            id=str(item.get("id") or _stable_id("coverage", item)),
            area=_text(item.get("area") or item.get("scope"), 500),
            status=_text(item.get("status"), 64) or "observed",
            evidence_refs=[str(ref) for ref in item.get("evidence_refs") or []],
            summary=_text(item.get("summary"), 500),
        )
        for item in data.get("coverage") or [] if isinstance(item, dict)
    ])
    return state


def migrate_legacy_task_state(
    payload: Any, *, source: str, original_request_ref: str = ""
) -> TaskState | None:
    if source == "working_memory":
        return migrate_working_memory_payload(payload, original_request_ref=original_request_ref)
    if source == "coding_context_state":
        return migrate_coding_context_state_payload(payload, original_request_ref=original_request_ref)
    raise ValueError(f"unsupported legacy state source: {source}")


def load_task_state(session: Any) -> TaskState | None:
    """Recover the newest valid TaskState from metadata or a checkpoint."""
    checkpoint_state: TaskState | None = None
    for checkpoint in list(getattr(session, "checkpoints", []) or []):
        if not isinstance(checkpoint, dict):
            continue
        payload = checkpoint.get("state")
        if isinstance(payload, dict) and isinstance(payload.get("task_state"), dict):
            restored = TaskState.from_payload(payload["task_state"])
            if restored is not None:
                checkpoint_state = restored
                break
    metadata = getattr(session, "metadata", {}) or {}
    metadata_state = TaskState.from_payload(metadata.get(TASK_STATE_METADATA_KEY))
    if metadata_state is not None and (
        checkpoint_state is None or metadata_state.version >= checkpoint_state.version
    ):
        return metadata_state
    return checkpoint_state


def save_task_state(session: Any, state: TaskState) -> TaskState:
    metadata = getattr(session, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
        session.metadata = metadata
    state.updated_at = _now()
    metadata[TASK_STATE_METADATA_KEY] = state.to_dict()
    # Legacy WorkingMemory is a one-time migration source, not a second writer.
    metadata.pop("working_memory", None)
    touch = getattr(session, "touch", None)
    if callable(touch):
        touch()
    return state


def ensure_task_state(
    session: Any,
    *,
    objective_summary: str,
    original_request_ref: str = "",
    artifact_refs: Iterable[str] = (),
    checkpoint_persister: Callable[..., Any] | None = None,
) -> TaskState:
    existing = load_task_state(session)
    if existing is not None:
        metadata = getattr(session, "metadata", {}) or {}
        persisted = _payload_dict(metadata.get(TASK_STATE_METADATA_KEY))
        if persisted is not None and persisted.get("schema") != TASK_STATE_SCHEMA:
            save_task_state(session, existing)
        return existing
    metadata = getattr(session, "metadata", {}) or {}
    source = ""
    source_payload: dict[str, Any] | None = None
    source_info: dict[str, Any] = {}
    working_memory_payload = _payload_dict(metadata.get("working_memory"))
    migrated = migrate_working_memory_payload(
        working_memory_payload, original_request_ref=original_request_ref
    )
    if migrated is not None:
        source = "working_memory"
        source_payload = working_memory_payload
    if migrated is None:
        coding_context_payload = _payload_dict(metadata.get("coding_context_state"))
        migrated = migrate_coding_context_state_payload(
            coding_context_payload, original_request_ref=original_request_ref
        )
        if migrated is not None:
            source = "coding_context_state"
            source_payload = coding_context_payload
    refs = [str(ref) for ref in artifact_refs if ref]
    if migrated is None:
        history = _latest_real_user_history(session)
        if history is not None:
            source = "session_history"
            source_payload = {
                "message": history["message"],
                "message_index": history["message_index"],
                "event_ref": history["event_ref"],
            }
            source_info = {
                "message_index": history["message_index"],
                "event_ref": history["event_ref"],
            }
            history_ref = _history_artifact_ref(history["message"])
            if history_ref:
                refs.append(history_ref)
            migrated = TaskState(objective=Objective(
                summary=_text(history["message"].get("content"), OBJECTIVE_SUMMARY_LIMIT),
                original_request_ref=(
                    original_request_ref or str(history["event_ref"] or "")
                ),
                source_artifacts=list(dict.fromkeys(refs)),
            ))
    state = migrated or TaskState(objective=Objective(
        summary=_text(objective_summary, OBJECTIVE_SUMMARY_LIMIT),
        original_request_ref=original_request_ref,
        source_artifacts=refs,
    ))
    state.artifact_refs = list(dict.fromkeys([*state.artifact_refs, *refs]))
    if not source:
        return save_task_state(session, state)

    previous = _migration_session_snapshot(session)
    try:
        save_task_state(session, state)
        if source == "coding_context_state":
            session.metadata.pop("coding_context_state", None)
        _persist_migration_checkpoint(
            session,
            state=state,
            source=source,
            source_payload=source_payload or {},
            source_info=source_info,
            checkpoint_persister=checkpoint_persister,
        )
    except Exception:
        _restore_migration_session_snapshot(session, previous)
        raise
    return state


def _persist_migration_checkpoint(
    session: Any,
    *,
    state: TaskState,
    source: str,
    source_payload: dict[str, Any],
    source_info: dict[str, Any],
    checkpoint_persister: Callable[..., Any] | None,
) -> dict[str, Any]:
    source_sha256 = _canonical_checksum(source_payload)
    state_payload = state.to_dict()
    migration = {
        "kind": "legacy_task_state_migration",
        "source": source,
        "source_sha256": source_sha256,
        "task_state_sha256": _canonical_checksum(state_payload),
        "task_state_version": TASK_STATE_VERSION,
        "source_info": dict(source_info),
    }
    for checkpoint in list(getattr(session, "checkpoints", []) or []):
        checkpoint_metadata = checkpoint.get("metadata") if isinstance(checkpoint, dict) else None
        existing = checkpoint_metadata.get("migration") if isinstance(checkpoint_metadata, dict) else None
        if isinstance(existing, dict) and (
            existing.get("kind") == migration["kind"]
            and existing.get("source") == source
            and existing.get("source_sha256") == source_sha256
        ):
            return checkpoint

    checkpoint_payload = {"task_state": state_payload, "migration": migration}
    boundary = max(0, int(getattr(session, "archive_boundary_seq", 0) or 0))
    persister = checkpoint_persister or getattr(
        session, "_context_checkpoint_persister", None
    )
    if callable(persister):
        return persister(
            session=session,
            checkpoint=checkpoint_payload,
            archive_boundary_seq=boundary,
            metadata={"migration": migration},
        )

    append_event = getattr(session, "append_event", None)
    checkpoints = getattr(session, "checkpoints", None)
    if not callable(append_event) or not isinstance(checkpoints, list):
        raise RuntimeError("task-state migration requires checkpoint persistence")
    created_at = _now()
    state_sha256 = _canonical_checksum(checkpoint_payload)
    checkpoint_id = f"migration:{source}:{source_sha256[:20]}"
    checkpoint_event = append_event(
        "task_state_checkpoint",
        {
            "checkpoint_id": checkpoint_id,
            "archive_boundary_seq": boundary,
            "state_sha256": state_sha256,
            "migration": migration,
        },
        created_at=created_at,
    )
    completion_event = append_event(
        "compaction_completed",
        {
            "checkpoint_id": checkpoint_id,
            "checkpoint_event_id": checkpoint_event.event_id,
            "archive_boundary_seq": boundary,
            "state_sha256": state_sha256,
            "migration": True,
        },
        created_at=created_at,
    )
    checkpoint = {
        "checkpoint_id": checkpoint_id,
        "archive_boundary_seq": boundary,
        "completion_event_id": completion_event.event_id,
        "created_at": created_at,
        "state": checkpoint_payload,
        "state_sha256": state_sha256,
        "metadata": {"migration": migration},
    }
    checkpoints.insert(0, checkpoint)
    return checkpoint


def _migration_session_snapshot(session: Any) -> dict[str, Any]:
    return {
        "metadata": deepcopy(getattr(session, "metadata", {}) or {}),
        "event_log": list(getattr(session, "event_log", []) or []),
        "checkpoints": deepcopy(getattr(session, "checkpoints", []) or []),
        "archive_boundary_seq": getattr(session, "archive_boundary_seq", 0),
        "last_compacted": getattr(session, "last_compacted", None),
        "updated_at": getattr(session, "updated_at", None),
    }


def _restore_migration_session_snapshot(session: Any, snapshot: dict[str, Any]) -> None:
    session.metadata = snapshot["metadata"]
    if hasattr(session, "event_log"):
        session.event_log = snapshot["event_log"]
    if hasattr(session, "checkpoints"):
        session.checkpoints = snapshot["checkpoints"]
    if hasattr(session, "archive_boundary_seq"):
        session.archive_boundary_seq = snapshot["archive_boundary_seq"]
    if hasattr(session, "last_compacted"):
        session.last_compacted = snapshot["last_compacted"]
    if hasattr(session, "updated_at"):
        session.updated_at = snapshot["updated_at"]
    refresh = getattr(session, "_refresh_active_event_window", None)
    if callable(refresh):
        refresh()


def _canonical_checksum(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _latest_real_user_history(session: Any) -> dict[str, Any] | None:
    messages = list(getattr(session, "messages", []) or [])
    for message_index in range(len(messages) - 1, -1, -1):
        message = messages[message_index]
        if not isinstance(message, dict) or str(message.get("role") or "") != "user":
            continue
        metadata = message.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        if str(metadata.get("source") or "").startswith("runtime-generated"):
            continue
        if not str(message.get("content") or "").strip():
            continue
        return {
            "message": dict(message),
            "message_index": message_index,
            "event_ref": _history_message_event_ref(session, message, message_index),
        }
    return None


def _history_message_event_ref(
    session: Any,
    message: dict[str, Any],
    message_index: int,
) -> str:
    fallback = ""
    for event in reversed(list(getattr(session, "event_log", []) or [])):
        if getattr(event, "type", "") not in {"user_message", "user_correction"}:
            continue
        payload = event.to_dict().get("payload", {})
        event_message = payload.get("message") if isinstance(payload, dict) else None
        if isinstance(event_message, dict) and event_message == message:
            return f"event://{event.event_id}"
        if payload.get("legacy_message_index") == message_index and not fallback:
            fallback = f"event://{event.event_id}"
    return fallback


def _history_artifact_ref(message: dict[str, Any]) -> str:
    metadata = message.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    ref = metadata.get("artifact_ref") or message.get("artifact_ref")
    if isinstance(ref, dict):
        return str(ref.get("storage_uri") or ref.get("artifact_id") or "")
    return str(ref or "")


def _payload_dict(payload: Any) -> dict[str, Any] | None:
    if hasattr(payload, "to_dict"):
        payload = payload.to_dict()
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    return dict(payload) if isinstance(payload, dict) else None


def _to_plain(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {key: _to_plain(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    return value


def _phase(value: Any) -> TaskPhase:
    aliases = {
        "explore": TaskPhase.EXPLORATION,
        "finalize": TaskPhase.FINALIZATION,
        "complete": TaskPhase.FINALIZATION,
        "completed": TaskPhase.FINALIZATION,
    }
    if str(value or "") in aliases:
        return aliases[str(value)]
    try:
        return TaskPhase(str(value or TaskPhase.INTAKE))
    except ValueError:
        return TaskPhase.INTAKE


def _status(value: Any) -> ItemStatus:
    value = str(value or ItemStatus.PENDING)
    value = {
        "todo": ItemStatus.PENDING,
        "running": ItemStatus.IN_PROGRESS,
        "dispatched": ItemStatus.IN_PROGRESS,
        "blocked": ItemStatus.FAILED,
        "cancelled": ItemStatus.SUPERSEDED,
    }.get(value, value)
    try:
        return ItemStatus(value)
    except ValueError:
        return ItemStatus.PENDING


def _task_status(value: Any, *, phase: Any = None) -> TaskStatus:
    if value is None and str(phase or "") == TaskPhase.BLOCKED.value:
        return TaskStatus.BLOCKED
    try:
        return TaskStatus(str(value or TaskStatus.ACTIVE))
    except ValueError:
        return TaskStatus.ACTIVE


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _merge_shared_core_payload(state: TaskState, payload: dict[str, Any]) -> None:
    """Apply a core-only update to a restored legacy coding extension."""
    objective = payload.get("objective")
    if isinstance(objective, dict):
        objective = objective.get("summary")
    if objective is not None:
        state.objective.summary = str(objective)
    state.task_id = str(payload.get("task_id") or state.task_id)
    state.version = max(1, int(payload.get("version") or state.version))
    state.status = _task_status(payload.get("status"), phase=payload.get("phase"))
    state.current_focus = _optional_text(payload.get("current_focus"))
    state.completion_basis = [
        str(item) for item in payload.get("completion_basis") or [] if item
    ]
    state.stop_reason = _optional_text(payload.get("stop_reason"))
    state.artifact_refs = [
        str(item) for item in payload.get("artifact_refs") or [] if item
    ]
    state.updated_at = str(payload.get("updated_at") or state.updated_at)
    if "constraints" in payload:
        state.constraints = _coerce_constraints(payload.get("constraints"))
    if "completed" in payload:
        state.completed = _coerce_list(CompletedItem, payload.get("completed"))
    if "pending_actions" in payload:
        state.pending_actions = _coerce_list(Action, payload.get("pending_actions"))
    if "open_questions" in payload:
        state.open_questions = _coerce_questions(payload.get("open_questions"))
    if "blockers" in payload:
        state.blockers = _coerce_list(Blocker, payload.get("blockers"))


def _coerce_list(type_: type, value: Any) -> list[Any]:
    return [_coerce(type_, item) for item in value or [] if isinstance(item, dict)]


def _coerce_constraints(value: Any) -> list[Constraint]:
    return [
        _coerce(Constraint, item)
        if isinstance(item, dict)
        else Constraint(f"core-constraint:{index}", str(item))
        for index, item in enumerate(value or [])
        if str(item).strip()
    ]


def _coerce_questions(value: Any) -> list[OpenQuestion]:
    return [
        _coerce(OpenQuestion, item)
        if isinstance(item, dict)
        else OpenQuestion(f"core-question:{index}", str(item))
        for index, item in enumerate(value or [])
        if str(item).strip()
    ]


def _coerce(type_: type, value: dict[str, Any]) -> Any:
    data = dict(value) if isinstance(value, dict) else {}
    if type_ is Decision and "choice" not in data and "summary" in data:
        data["choice"] = data.get("summary")
    if type_ is Objective:
        data.setdefault("summary", "")
    if type_ in {Constraint, PlanItem, Action, OpenQuestion, Blocker, Hypothesis, Finding, Decision}:
        if "status" in data:
            data["status"] = _status(data["status"])
    if "evidence_refs" in data:
        data["evidence_refs"] = _refs(data["evidence_refs"])
    if type_ is CoverageState:
        return CoverageState(
            entries=_coerce_list(CoverageEntry, data.get("entries")),
            uncovered=[str(item) for item in data.get("uncovered") or []],
            files_inspected=[str(item) for item in data.get("files_inspected") or []],
            symbols_inspected=[str(item) for item in data.get("symbols_inspected") or []],
            files_modified=[str(item) for item in data.get("files_modified") or []],
            tests_run=[str(item) for item in data.get("tests_run") or []],
            sections_inspected=[str(item) for item in data.get("sections_inspected") or []],
            ranges_read=[str(item) for item in data.get("ranges_read") or []],
            samples_checked=[str(item) for item in data.get("samples_checked") or []],
            areas_unchecked=[str(item) for item in data.get("areas_unchecked") or []],
        )
    return type_(**{key: value for key, value in data.items() if key in type_.__dataclass_fields__})


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _refs(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value if item] if isinstance(value, list) else []
