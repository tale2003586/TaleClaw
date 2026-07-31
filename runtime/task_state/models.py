"""Mode-neutral task lifecycle and progress models.

The schema version describes the persisted envelope. ``TaskStateCore.version``
is the optimistic-concurrency revision and is incremented by reducers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import StrEnum
import json
from typing import Any, Mapping


TASK_STATE_METADATA_KEY = "task_state"
TASK_STATE_SCHEMA = "task_state"
TASK_STATE_SCHEMA_VERSION = 2
TASK_STATE_INITIAL_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStatus(StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
}


@dataclass
class TaskProgressItem:
    id: str
    description: str
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class TaskAction:
    id: str
    description: str
    status: str = "pending"
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class TaskBlocker:
    id: str
    description: str
    evidence_refs: list[str] = field(default_factory=list)
    resolution_strategy: str = ""


@dataclass
class TaskStateCore:
    """The single mutable task authority shared by every runtime mode.

    CodingTaskState subclasses this model and refines the common collection
    item types while adding its code-specific extension fields.
    """

    task_id: str = ""
    version: int = TASK_STATE_INITIAL_VERSION
    objective: str = ""
    constraints: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.ACTIVE
    current_focus: str | None = None
    completed: list[TaskProgressItem] = field(default_factory=list)
    pending_actions: list[TaskAction] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    blockers: list[TaskBlocker] = field(default_factory=list)
    completion_basis: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    artifact_refs: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=utc_now)

    def core_dict(self) -> dict[str, Any]:
        return _plain(self)

    def to_dict(self) -> dict[str, Any]:
        return task_state_envelope(self)

    @classmethod
    def from_payload(cls, payload: Any) -> "TaskStateCore | None":
        data = payload_dict(payload)
        if data is None:
            return None
        if data.get("schema") == TASK_STATE_SCHEMA:
            core = payload_dict(data.get("core"))
            if core is None:
                return None
            data = core
        objective = data.get("objective")
        if isinstance(objective, Mapping):
            objective = objective.get("summary")
        if objective is None:
            return None
        try:
            status = _task_status(data.get("status"), phase=data.get("phase"))
            return cls(
                task_id=str(data.get("task_id") or ""),
                version=max(1, int(data.get("version") or TASK_STATE_INITIAL_VERSION)),
                objective=str(objective or "").strip(),
                constraints=[
                    _constraint_text(item)
                    for item in data.get("constraints") or []
                    if _constraint_text(item)
                ],
                status=status,
                current_focus=_optional_text(data.get("current_focus")),
                completed=[
                    _progress_item(item, index)
                    for index, item in enumerate(data.get("completed") or [])
                    if isinstance(item, (str, Mapping))
                ],
                pending_actions=[
                    _action(item, index)
                    for index, item in enumerate(data.get("pending_actions") or [])
                    if isinstance(item, (str, Mapping))
                ],
                open_questions=[
                    str(item.get("question") if isinstance(item, Mapping) else item)
                    for item in data.get("open_questions") or []
                    if str(item.get("question") if isinstance(item, Mapping) else item).strip()
                ],
                blockers=[
                    _blocker(item, index)
                    for index, item in enumerate(data.get("blockers") or [])
                    if isinstance(item, (str, Mapping))
                ],
                completion_basis=[
                    str(item) for item in data.get("completion_basis") or [] if str(item).strip()
                ],
                stop_reason=_optional_text(data.get("stop_reason")),
                artifact_refs=list(dict.fromkeys(
                    str(item) for item in data.get("artifact_refs") or [] if str(item).strip()
                )),
                updated_at=str(data.get("updated_at") or utc_now()),
            )
        except (TypeError, ValueError):
            return None


def task_state_envelope(
    core: TaskStateCore,
    *,
    extensions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return TaskStateEnvelope({
        "schema": TASK_STATE_SCHEMA,
        "schema_version": TASK_STATE_SCHEMA_VERSION,
        "core": core.core_dict(),
        "extensions": _plain(dict(extensions or {})),
    })


class TaskStateEnvelope(dict[str, Any]):
    """V2 envelope with non-serialized read-through aliases for legacy callers.

    The aliases are views into ``core``/``extensions.coding`` rather than a
    duplicated persisted state, so the v2 fields remain authoritative.
    """

    def __getitem__(self, key: str) -> Any:
        try:
            return super().__getitem__(key)
        except KeyError:
            core = super().get("core")
            if isinstance(core, dict) and key in core:
                return core[key]
            extensions = super().get("extensions")
            coding = extensions.get("coding") if isinstance(extensions, dict) else None
            if isinstance(coding, dict) and key in coding:
                return coding[key]
            raise

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: object) -> bool:
        if super().__contains__(key):
            return True
        if not isinstance(key, str):
            return False
        core = super().get("core")
        if isinstance(core, dict) and key in core:
            return True
        extensions = super().get("extensions")
        coding = extensions.get("coding") if isinstance(extensions, dict) else None
        return isinstance(coding, dict) and key in coding


def payload_dict(payload: Any) -> dict[str, Any] | None:
    if hasattr(payload, "to_dict"):
        payload = payload.to_dict()
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, Mapping):
        return None
    return dict(payload)


def _plain(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    return value


def _task_status(value: Any, *, phase: Any = None) -> TaskStatus:
    if value is None and str(phase or "") == "blocked":
        return TaskStatus.BLOCKED
    try:
        return TaskStatus(str(value or TaskStatus.ACTIVE))
    except ValueError:
        return TaskStatus.ACTIVE


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _constraint_text(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("text")
    return str(value or "").strip()


def _progress_item(value: str | Mapping[str, Any], index: int) -> TaskProgressItem:
    data = dict(value) if isinstance(value, Mapping) else {"description": value}
    return TaskProgressItem(
        id=str(data.get("id") or f"progress:{index}"),
        description=str(data.get("description") or data.get("summary") or "").strip(),
        evidence_refs=[str(item) for item in data.get("evidence_refs") or [] if item],
    )


def _action(value: str | Mapping[str, Any], index: int) -> TaskAction:
    data = dict(value) if isinstance(value, Mapping) else {"description": value}
    return TaskAction(
        id=str(data.get("id") or f"action:{index}"),
        description=str(data.get("description") or data.get("summary") or "").strip(),
        status=str(data.get("status") or "pending"),
        evidence_refs=[str(item) for item in data.get("evidence_refs") or [] if item],
    )


def _blocker(value: str | Mapping[str, Any], index: int) -> TaskBlocker:
    data = dict(value) if isinstance(value, Mapping) else {"description": value}
    return TaskBlocker(
        id=str(data.get("id") or f"blocker:{index}"),
        description=str(data.get("description") or data.get("summary") or "").strip(),
        evidence_refs=[str(item) for item in data.get("evidence_refs") or [] if item],
        resolution_strategy=str(data.get("resolution_strategy") or "").strip(),
    )
