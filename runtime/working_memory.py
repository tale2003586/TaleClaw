from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any


WORKING_MEMORY_METADATA_KEY = "working_memory"
WORKING_MEMORY_RESUME_REQUESTED_KEY = "working_memory_resume_requested"

STATUS_RUNNING = "running"
STATUS_SUSPENDED = "suspended"
STATUS_COMPLETED = "completed"

_RESUME_MARKERS = (
    "/resume",
    "resume",
    "继续",
    "续做",
    "断点",
    "接着",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WorkingMemory:
    task_id: str
    objective: str
    completed_units: list[dict[str, Any]] = field(default_factory=list)
    pending_units: list[dict[str, Any]] = field(default_factory=list)
    archived_findings: dict[str, Any] = field(default_factory=dict)
    last_checkpoint_step: int = 0
    status: str = STATUS_RUNNING
    updated_at: str = field(default_factory=_now_iso)

    @classmethod
    def from_payload(cls, payload: Any) -> "WorkingMemory | None":
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return None
        if not isinstance(payload, dict):
            return None
        return cls(
            task_id=str(payload.get("task_id") or ""),
            objective=str(payload.get("objective") or ""),
            completed_units=_list_of_dicts(payload.get("completed_units")),
            pending_units=_list_of_dicts(payload.get("pending_units")),
            archived_findings=(
                dict(payload.get("archived_findings"))
                if isinstance(payload.get("archived_findings"), dict)
                else {}
            ),
            last_checkpoint_step=_int(payload.get("last_checkpoint_step"), 0),
            status=str(payload.get("status") or STATUS_RUNNING),
            updated_at=str(payload.get("updated_at") or _now_iso()),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_resume_request(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    return any(marker in lowered for marker in _RESUME_MARKERS)


def load_working_memory(session) -> WorkingMemory | None:
    metadata = getattr(session, "metadata", {}) or {}
    return WorkingMemory.from_payload(metadata.get(WORKING_MEMORY_METADATA_KEY))


def save_working_memory(session, memory: WorkingMemory) -> WorkingMemory:
    memory.updated_at = _now_iso()
    metadata = _metadata_for(session)
    metadata[WORKING_MEMORY_METADATA_KEY] = memory.to_dict()
    touch = getattr(session, "touch", None)
    if touch is not None:
        touch()
    return memory


def prepare_working_memory_for_turn(
    session,
    *,
    objective: str,
    resume_requested: bool = False,
    task_id: str | None = None,
) -> WorkingMemory:
    existing = load_working_memory(session)
    objective = str(objective or "").strip()
    if existing is None or existing.status == STATUS_COMPLETED:
        existing = WorkingMemory(
            task_id=task_id or getattr(session, "id", "") or _stable_id(objective),
            objective=objective,
            status=STATUS_RUNNING,
        )
    else:
        if not existing.task_id:
            existing.task_id = task_id or getattr(session, "id", "") or _stable_id(objective)
        if not existing.objective and objective:
            existing.objective = objective
        existing.status = STATUS_RUNNING
    _metadata_for(session)[WORKING_MEMORY_RESUME_REQUESTED_KEY] = bool(resume_requested)
    return save_working_memory(session, existing)


def inherit_working_memory(
    *,
    source_session,
    target_session,
    objective: str,
    task_id: str | None = None,
) -> WorkingMemory:
    source = load_working_memory(source_session)
    if source is None or source.status == STATUS_COMPLETED:
        memory = WorkingMemory(
            task_id=task_id or getattr(target_session, "id", "") or _stable_id(objective),
            objective=str(objective or "").strip(),
            status=STATUS_RUNNING,
        )
    else:
        memory = WorkingMemory.from_payload(source.to_dict()) or source
        memory.status = STATUS_RUNNING
        if not memory.objective:
            memory.objective = str(objective or "").strip()
        if task_id:
            memory.task_id = task_id
    resume_requested = bool(
        (getattr(source_session, "metadata", {}) or {}).get(
            WORKING_MEMORY_RESUME_REQUESTED_KEY
        )
    )
    _metadata_for(target_session)[WORKING_MEMORY_RESUME_REQUESTED_KEY] = resume_requested
    return save_working_memory(target_session, memory)


def sync_working_memory(*, source_session, target_session) -> WorkingMemory | None:
    memory = load_working_memory(source_session)
    if memory is None:
        return None
    return save_working_memory(target_session, memory)


def checkpoint_subtasks_dispatched(
    session,
    tasks: list[dict[str, Any]],
    *,
    step: int | None = None,
) -> WorkingMemory | None:
    if not tasks:
        return load_working_memory(session)
    memory = _ensure_memory(session)
    if step is not None:
        memory.last_checkpoint_step = max(memory.last_checkpoint_step, _int(step, 0))
    for index, task in enumerate(tasks):
        unit_id = _unit_id(task, index=index)
        _upsert_pending(
            memory,
            {
                "unit_id": unit_id,
                "description": _task_description(task),
                "scope_files": _scope_files(task),
                "agent_type": str(task.get("agent_type") or ""),
                "status": "dispatched",
            },
        )
    memory.status = STATUS_RUNNING
    return save_working_memory(session, memory)


def checkpoint_subtask_results(
    session,
    tasks: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    step: int | None = None,
) -> WorkingMemory | None:
    if not tasks and not results:
        return load_working_memory(session)
    memory = _ensure_memory(session)
    if step is not None:
        memory.last_checkpoint_step = max(memory.last_checkpoint_step, _int(step, 0))
    for index, task in enumerate(tasks or []):
        result = results[index] if index < len(results or []) else {}
        unit_id = _unit_id(task, index=index)
        memory.archived_findings[unit_id] = _archive_payload(task, result)
        if _result_completed(result):
            _remove_pending(memory, unit_id)
            _upsert_completed(
                memory,
                {
                    "unit_id": unit_id,
                    "description": _task_description(task),
                    "conclusion": _result_conclusion(result),
                    "evidence_refs": _evidence_refs(result),
                    "agent_type": str(result.get("agent_type") or task.get("agent_type") or ""),
                    "status": str(result.get("status") or "completed"),
                },
            )
        else:
            _upsert_pending(
                memory,
                {
                    "unit_id": unit_id,
                    "description": _task_description(task),
                    "scope_files": _scope_files(task),
                    "agent_type": str(task.get("agent_type") or ""),
                    "status": str(result.get("status") or "pending"),
                    "last_failure_reason": str(
                        result.get("failure_reason") or result.get("stop_reason") or ""
                    ),
                    "last_failure_message": _clip(
                        str(result.get("failure_message") or result.get("error") or ""),
                        500,
                    ),
                },
            )
    memory.status = STATUS_RUNNING
    return save_working_memory(session, memory)


def checkpoint_turn_stopped(
    session,
    *,
    reason: str,
    message: str,
    step: int | None = None,
) -> WorkingMemory:
    memory = _ensure_memory(session)
    if step is not None:
        memory.last_checkpoint_step = max(memory.last_checkpoint_step, _int(step, 0))
    memory.status = STATUS_SUSPENDED
    memory.archived_findings["last_stop"] = {
        "reason": str(reason or ""),
        "message": _clip(message, 1200),
        "timestamp": _now_iso(),
    }
    return save_working_memory(session, memory)


def complete_working_memory(
    session,
    *,
    final_answer: str,
    step: int | None = None,
) -> WorkingMemory | None:
    memory = load_working_memory(session)
    if memory is None:
        return None
    if step is not None:
        memory.last_checkpoint_step = max(memory.last_checkpoint_step, _int(step, 0))
    memory.status = STATUS_COMPLETED
    memory.archived_findings["final_answer"] = {
        "summary": _clip(final_answer, 2000),
        "timestamp": _now_iso(),
    }
    return save_working_memory(session, memory)


def partial_summary(session) -> str:
    memory = load_working_memory(session)
    lines = [
        "本轮已按用户请求停止。当前工具调用如果已经开始，会在完整结束后再停在这个边界。",
    ]
    if memory is not None:
        lines.append("")
        lines.append("已保存工作记忆，可稍后发送“继续”或“resume”基于断点续做。")
        if memory.completed_units:
            lines.append("")
            lines.append("已完成的线索：")
            for unit in memory.completed_units[:8]:
                lines.append(
                    f"- {unit.get('unit_id')}: {_clip(unit.get('conclusion', ''), 220)}"
                )
        if memory.pending_units:
            lines.append("")
            lines.append("待继续的线索：")
            for unit in memory.pending_units[:8]:
                lines.append(
                    f"- {unit.get('unit_id')}: {_clip(unit.get('description', ''), 220)}"
                )
        return "\n".join(lines)

    latest = _latest_assistant_or_tool_text(getattr(session, "messages", []) or [])
    if latest:
        lines.extend(["", "最近可用进展：", _clip(latest, 1200)])
    else:
        lines.append("")
        lines.append("目前还没有可汇总的模型输出或工具结果。")
    return "\n".join(lines)


def render_working_memory_block(session) -> str:
    memory = load_working_memory(session)
    if memory is None or memory.status == STATUS_COMPLETED:
        return ""
    lines = [
        f"<working-memory task_id=\"{_xml_attr(memory.task_id)}\" status=\"{_xml_attr(memory.status)}\">",
        f"原始任务: {memory.objective or '(unknown)'}",
        f"最后检查点步骤: {memory.last_checkpoint_step}",
        "",
        "已完成:",
    ]
    if memory.completed_units:
        for unit in memory.completed_units:
            evidence = unit.get("evidence_refs") or []
            evidence_text = ", ".join(str(item) for item in evidence[:8])
            lines.append(
                f"- [{unit.get('unit_id')}] {unit.get('description') or ''}\n"
                f"  结论: {unit.get('conclusion') or ''}\n"
                f"  证据: {evidence_text or '(none)'}"
            )
    else:
        lines.append("- (none)")
    lines.extend(["", "待办:"])
    if memory.pending_units:
        for unit in memory.pending_units:
            scope = ", ".join(str(item) for item in (unit.get("scope_files") or [])[:12])
            failure = unit.get("last_failure_reason") or ""
            suffix = f" last_failure={failure}" if failure else ""
            lines.append(
                f"- [{unit.get('unit_id')}] {unit.get('description') or ''}"
                f"{suffix}\n  scope_files: {scope or '(unspecified)'}"
            )
    else:
        lines.append("- (none)")
    if memory.archived_findings:
        lines.extend(["", "归档发现:"])
        for key, value in list(memory.archived_findings.items())[:12]:
            lines.append(f"- {key}: {_clip(json.dumps(value, ensure_ascii=False, default=str), 900)}")
    lines.extend([
        "</working-memory>",
        "",
        "<working-memory-instruction critical=\"true\">",
        "基于已完成部分继续完成任务；不要重做已完成线索。优先处理待办线索，最终汇总必须合并已完成结论和新完成结论。",
        "</working-memory-instruction>",
    ])
    return "\n".join(lines)


def _ensure_memory(session) -> WorkingMemory:
    memory = load_working_memory(session)
    if memory is None:
        memory = WorkingMemory(
            task_id=getattr(session, "id", "") or "task",
            objective=_latest_user_text(getattr(session, "messages", []) or []),
            status=STATUS_RUNNING,
        )
    return memory


def _metadata_for(session) -> dict[str, Any]:
    metadata = getattr(session, "metadata", None)
    if metadata is None:
        metadata = {}
        setattr(session, "metadata", metadata)
    return metadata


def _upsert_pending(memory: WorkingMemory, unit: dict[str, Any]) -> None:
    unit_id = str(unit.get("unit_id") or "")
    for index, existing in enumerate(memory.pending_units):
        if str(existing.get("unit_id") or "") == unit_id:
            merged = dict(existing)
            merged.update({key: value for key, value in unit.items() if value not in (None, "")})
            memory.pending_units[index] = merged
            return
    memory.pending_units.append(unit)


def _upsert_completed(memory: WorkingMemory, unit: dict[str, Any]) -> None:
    unit_id = str(unit.get("unit_id") or "")
    for index, existing in enumerate(memory.completed_units):
        if str(existing.get("unit_id") or "") == unit_id:
            merged = dict(existing)
            merged.update(unit)
            memory.completed_units[index] = merged
            return
    memory.completed_units.append(unit)


def _remove_pending(memory: WorkingMemory, unit_id: str) -> None:
    memory.pending_units = [
        unit
        for unit in memory.pending_units
        if str(unit.get("unit_id") or "") != unit_id
    ]


def _archive_payload(task: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": {
            "description": _task_description(task),
            "scope_files": _scope_files(task),
            "agent_type": str(task.get("agent_type") or ""),
        },
        "result": {
            "success": bool(result.get("success")),
            "status": str(result.get("status") or ""),
            "summary": _clip(str(result.get("summary") or ""), 1800),
            "findings": result.get("findings") or [],
            "files_touched": result.get("files_touched") or [],
            "failure_reason": str(result.get("failure_reason") or ""),
            "stop_reason": str(result.get("stop_reason") or ""),
        },
        "timestamp": _now_iso(),
    }


def _result_completed(result: dict[str, Any]) -> bool:
    return bool(result.get("success")) and not bool(result.get("incomplete"))


def _result_conclusion(result: dict[str, Any]) -> str:
    findings = result.get("findings")
    summary = str(result.get("summary") or "").strip()
    if findings:
        return _clip(
            (summary + "\n" if summary else "")
            + json.dumps(findings, ensure_ascii=False, default=str),
            2200,
        )
    return _clip(summary, 2200)


def _evidence_refs(result: dict[str, Any]) -> list[str]:
    refs = []
    for item in result.get("files_touched") or []:
        refs.append(str(item))
    for item in result.get("evidence") or []:
        if isinstance(item, dict):
            path = item.get("path") or item.get("file") or item.get("source")
            if path:
                refs.append(str(path))
        elif item:
            refs.append(str(item))
    return list(dict.fromkeys(refs))


def _unit_id(task: dict[str, Any], *, index: int) -> str:
    explicit = task.get("unit_id") or task.get("id")
    if explicit:
        return _slug(str(explicit))
    description = _task_description(task)
    scope = json.dumps(task.get("scope") or {}, ensure_ascii=False, sort_keys=True, default=str)
    seed = f"{index}:{description}:{scope}:{task.get('objective') or ''}"
    return f"unit-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:8]}"


def _task_description(task: dict[str, Any]) -> str:
    for key in ("description", "objective", "deliverable", "prompt"):
        value = str(task.get(key) or "").strip()
        if value:
            return _clip(value, 600)
    return "(unnamed subtask)"


def _scope_files(task: dict[str, Any]) -> list[str]:
    scope = task.get("scope")
    if not isinstance(scope, dict):
        return []
    files = scope.get("files")
    if not isinstance(files, list):
        return []
    return [str(item) for item in files if str(item or "").strip()]


def _latest_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _message_text(message)
    return ""


def _latest_assistant_or_tool_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") in {"assistant", "tool"}:
            text = _message_text(message).strip()
            if text:
                return text
    return ""


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _stable_id(text: str) -> str:
    return "task-" + hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()[:8]


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower())
    return slug.strip("-") or _stable_id(value)


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _xml_attr(value: Any) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)
