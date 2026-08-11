"""Read-only adapter for persisted task state formats retired from Runtime.

Delete this module after supported session stores no longer contain either
legacy metadata key and the migration checkpoint retention window has elapsed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping

from .models import (
    TaskAction,
    TaskProgressItem,
    TaskStateCore,
    TaskStatus,
)


LEGACY_WORKING_MEMORY_KEY = "working_memory"
LEGACY_CODING_CONTEXT_KEY = "coding_context_state"
LEGACY_TASK_STATE_KEYS = (
    LEGACY_WORKING_MEMORY_KEY,
    LEGACY_CODING_CONTEXT_KEY,
)


@dataclass(frozen=True)
class LegacyTaskState:
    source: str
    source_payload: dict[str, Any]
    task_id: str
    objective: str
    original_request_ref: str
    finish_condition: str
    status: TaskStatus
    phase: str
    completed: tuple[dict[str, Any], ...]
    pending_actions: tuple[dict[str, Any], ...]
    open_questions: tuple[str, ...]
    archived_findings: tuple[tuple[str, str], ...]
    evidence_index: tuple[tuple[str, dict[str, Any]], ...]
    findings: tuple[dict[str, Any], ...]
    coverage: tuple[dict[str, Any], ...]
    updated_at: str

    def to_core(self) -> TaskStateCore:
        return TaskStateCore(
            task_id=self.task_id,
            objective=self.objective,
            status=self.status,
            completed=[
                TaskProgressItem(
                    id=str(item["id"]),
                    description=str(item["description"]),
                    evidence_refs=list(item["evidence_refs"]),
                )
                for item in self.completed
            ],
            pending_actions=[
                TaskAction(
                    id=str(item["id"]),
                    description=str(item["description"]),
                    status=str(item["status"]),
                    evidence_refs=list(item["evidence_refs"]),
                )
                for item in self.pending_actions
            ],
            open_questions=list(self.open_questions),
            current_focus="Resume migrated task progress",
            updated_at=self.updated_at,
        )


def read_legacy_task_state(
    metadata: Mapping[str, Any],
    *,
    original_request_ref: str = "",
) -> LegacyTaskState | None:
    """Read the first supported legacy payload without mutating metadata."""

    for source in LEGACY_TASK_STATE_KEYS:
        parsed = parse_legacy_task_state(
            metadata.get(source),
            source=source,
            original_request_ref=original_request_ref,
        )
        if parsed is not None:
            return parsed
    return None


def parse_legacy_task_state(
    payload: Any,
    *,
    source: str,
    original_request_ref: str = "",
) -> LegacyTaskState | None:
    """Normalize one retired payload; never writes or returns legacy objects."""

    if source not in LEGACY_TASK_STATE_KEYS:
        raise ValueError(f"unsupported legacy task state source: {source}")
    data = _mapping(payload)
    if data is None or not _text(data.get("objective"), 480):
        return None

    working = source == LEGACY_WORKING_MEMORY_KEY
    completed_source = data.get("completed_units") if working else data.get("completed")
    pending_source = data.get("pending_units") if working else data.get("pending_actions")
    completed = tuple(
        {
            "id": str(item.get("unit_id") or item.get("id") or f"legacy-progress:{index}"),
            "description": _text(
                item.get("conclusion") or item.get("description") or item.get("summary")
            ),
            "evidence_refs": _refs(item.get("evidence_refs") or item.get("evidence")),
        }
        for index, item in enumerate(completed_source or ())
        if isinstance(item, Mapping)
    )
    pending = tuple(
        {
            "id": str(item.get("unit_id") or item.get("id") or f"legacy-action:{index}"),
            "description": _text(item.get("description") or item.get("summary")),
            "status": _status(item.get("state") or item.get("status")),
            "priority": _text(item.get("priority"), 8) or "P1",
            "evidence_refs": _refs(item.get("evidence_refs") or item.get("evidence")),
        }
        for index, item in enumerate(pending_source or ())
        if isinstance(item, Mapping)
    )
    evidence = _mapping(data.get("evidence_index")) or {}
    archived = _mapping(data.get("archived_findings")) or {}
    phase = str(data.get("phase") or "")
    status = (
        TaskStatus.COMPLETED
        if working and str(data.get("status") or "") == "completed"
        else TaskStatus.BLOCKED
        if not working and phase == "blocked"
        else TaskStatus.ACTIVE
    )
    return LegacyTaskState(
        source=source,
        source_payload=data,
        task_id=str(data.get("task_id") or ""),
        objective=_text(data.get("objective"), 480),
        original_request_ref=str(original_request_ref or ""),
        finish_condition=_text(data.get("finish_condition"), 480),
        status=status,
        phase=phase,
        completed=completed,
        pending_actions=pending,
        open_questions=tuple(
            _text(item.get("question") if isinstance(item, Mapping) else item, 600)
            for item in data.get("open_questions") or ()
            if _text(item.get("question") if isinstance(item, Mapping) else item, 600)
        ),
        archived_findings=tuple(
            (str(key), _text(value, 500))
            for key, value in archived.items()
            if key not in {"last_reasoning_step", "last_stop", "final_answer"}
        ),
        evidence_index=tuple(
            (str(key), dict(value))
            for key, value in evidence.items()
            if isinstance(value, Mapping)
        ),
        findings=tuple(
            dict(item) for item in data.get("findings") or () if isinstance(item, Mapping)
        ),
        coverage=tuple(
            dict(item) for item in data.get("coverage") or () if isinstance(item, Mapping)
        ),
        updated_at=str(data.get("updated_at") or "migration:unknown-time"),
    )


def remove_legacy_task_state_keys(metadata: dict[str, Any]) -> None:
    for key in LEGACY_TASK_STATE_KEYS:
        metadata.pop(key, None)


def persist_task_state_migration_checkpoint(
    session: Any,
    *,
    task_state_payload: dict[str, Any],
    source: str,
    source_payload: Mapping[str, Any],
    checkpoint_persister=None,
    source_info: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Checkpoint normalized state before callers remove retired metadata keys."""

    migration = {
        "kind": "legacy_task_state_migration",
        "source": source,
        "source_sha256": _checksum(source_payload),
        "task_state_sha256": _checksum(task_state_payload),
        "task_state_version": 1,
        "source_info": dict(source_info or {}),
    }
    for checkpoint in list(getattr(session, "checkpoints", []) or []):
        metadata = checkpoint.get("metadata") if isinstance(checkpoint, Mapping) else None
        existing = metadata.get("migration") if isinstance(metadata, Mapping) else None
        if isinstance(existing, Mapping) and all(
            existing.get(key) == migration[key]
            for key in ("kind", "source", "source_sha256")
        ):
            return dict(checkpoint)

    checkpoint_payload = {"task_state": task_state_payload, "migration": migration}
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

    checkpoints = getattr(session, "checkpoints", None)
    if not isinstance(checkpoints, list):
        raise RuntimeError("task-state migration requires checkpoint persistence")
    created_at = datetime.now(timezone.utc).isoformat()
    state_sha256 = _checksum(checkpoint_payload)
    checkpoint_id = f"migration:{source}:{migration['source_sha256'][:20]}"
    checkpoint = {
        "checkpoint_id": checkpoint_id,
        "archive_boundary_seq": boundary,
        "completion_event_id": "",
        "created_at": created_at,
        "state": checkpoint_payload,
        "state_sha256": state_sha256,
        "metadata": {"migration": migration},
    }
    checkpoints.insert(0, checkpoint)
    return checkpoint


def _mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return dict(value) if isinstance(value, Mapping) else None


def _text(value: Any, limit: int = 800) -> str:
    if isinstance(value, Mapping):
        value = value.get("summary")
    return str(value or "").strip()[:limit]


def _refs(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    return [str(item) for item in value or () if str(item)]


def _status(value: Any) -> str:
    normalized = str(value or "pending").lower()
    return {
        "todo": "pending",
        "active": "in_progress",
        "done": "completed",
    }.get(normalized, normalized)


def _checksum(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
