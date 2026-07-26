from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

from runtime.tooling.signature import tool_call_signature, tool_result_hash


WORKING_MEMORY_METADATA_KEY = "working_memory"
WORKING_MEMORY_RESUME_REQUESTED_KEY = "working_memory_resume_requested"
STEP_CHECKPOINT_HISTORY_LIMIT = 50
OBSERVED_CALLS_LIMIT = 30
OBSERVED_GIST_CHARS = 200

PRIORITIES = ("P0", "P1", "P2")
PENDING_STATES = ("todo", "in_progress", "blocked")

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
    step_checkpoints: list[dict[str, Any]] = field(default_factory=list)
    observed_calls: list[dict[str, Any]] = field(default_factory=list)
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
            pending_units=_normalize_pending_units(payload.get("pending_units")),
            archived_findings=(
                dict(payload.get("archived_findings"))
                if isinstance(payload.get("archived_findings"), dict)
                else {}
            ),
            step_checkpoints=_list_of_dicts(payload.get("step_checkpoints")),
            observed_calls=_normalize_observed_calls(payload.get("observed_calls")),
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
    include_pending_units: bool = True,
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
        if not include_pending_units:
            memory.pending_units = []
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
                "priority": _task_priority(task),
                "state": "in_progress",
                "blocked_by": _task_blocked_by(task),
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
                    "covered_scope": _list_of_strings(result.get("covered_scope")),
                    "open_questions": _list_of_strings(result.get("open_questions")),
                    "needs_parent_verification": bool(result.get("needs_parent_verification")),
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
                    "priority": _task_priority(task),
                    "state": _pending_state(
                        result.get("status") or "pending",
                        failure_reason=(
                            result.get("failure_reason")
                            or result.get("stop_reason")
                            or result.get("error")
                        ),
                    ),
                    "blocked_by": _task_blocked_by(task),
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


def checkpoint_reasoning_step(
    session,
    *,
    step: int,
    phase: str,
    message_count: int | None = None,
    assistant_summary: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    tool_results: list[dict[str, Any]] | None = None,
    note: str = "",
) -> WorkingMemory:
    memory = _ensure_memory(session)
    step_value = _int(step, 0)
    if step_value > 0:
        memory.last_checkpoint_step = max(memory.last_checkpoint_step, step_value)
    memory.status = STATUS_RUNNING
    checkpoint = {
        "step": step_value,
        "phase": str(phase or ""),
        "message_count": _int(message_count, 0) if message_count is not None else None,
        "assistant_summary": _clip(assistant_summary, 1000),
        "tool_calls": _compact_tool_calls(tool_calls or []),
        "tool_results": _compact_tool_results(tool_results or []),
        "note": _clip(note, 500),
        "timestamp": _now_iso(),
    }
    checkpoint = {
        key: value
        for key, value in checkpoint.items()
        if value not in (None, "", [])
    }
    _upsert_step_checkpoint(memory, checkpoint)
    _record_observed_calls(
        memory,
        tool_calls=tool_calls or [],
        tool_results=tool_results or [],
        step=step_value,
    )
    memory.archived_findings["last_reasoning_step"] = checkpoint
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
    metadata = getattr(session, "metadata", {}) or {}
    if str(metadata.get("kind") or "") == "subagent":
        return _render_subagent_working_memory_block(session, memory)
    lines = [
        f"<working-memory task_id=\"{_xml_attr(memory.task_id)}\" status=\"{_xml_attr(memory.status)}\">",
        f"原始任务: {memory.objective or '(unknown)'}",
        f"最后检查点步骤: {memory.last_checkpoint_step}",
        "",
        "下一步动作队列（有多个独立 ready units 时优先 parallel_tasks）:",
    ]
    queue_lines = _render_action_queue(memory.pending_units)
    lines.extend(queue_lines)

    lines.extend(["", "已观察（不要重复这些调用，直接用结论）:"])
    observed_lines = _render_observed_calls(memory.observed_calls)
    lines.extend(observed_lines)

    lines.extend(["", "已完成:"])
    if memory.completed_units:
        for unit in memory.completed_units[:8]:
            evidence = unit.get("evidence_refs") or []
            evidence_text = ", ".join(str(item) for item in evidence[:8])
            open_questions = unit.get("open_questions") or []
            verification = "yes" if unit.get("needs_parent_verification") else "no"
            lines.append(
                f"- [{unit.get('unit_id')}] {unit.get('description') or ''}\n"
                f"  结论: {unit.get('conclusion') or ''}\n"
                f"  证据: {evidence_text or '(none)'}\n"
                f"  需要父级复核: {verification}\n"
                f"  未解问题: {', '.join(str(item) for item in open_questions[:5]) or '(none)'}"
            )
    else:
        lines.append("- (none)")
    if memory.archived_findings:
        lines.extend(["", "归档发现:"])
        for key, value in list(memory.archived_findings.items())[:8]:
            lines.append(f"- {key}: {_clip(json.dumps(value, ensure_ascii=False, default=str), 900)}")
    lines.extend([
        "</working-memory>",
        "",
        "<working-memory-protocol critical=\"true\">",
        "每个推理步开始前，按顺序执行：",
        "1. 读“已观察”账本。如果相同 signature 已观察过，且目标文件/目录未被修改，优先复用结论；如果相关文件已被修改，则允许重读。",
        "2. 如果队列中有 “PARALLEL” 或多个非 blocked 且 scope 不重叠的 ready units，优先调用 parallel_tasks 批量分派；每个子任务必须有清晰 scope、objective、deliverable。",
        "3. 如果没有可并行项，推进 NEXT；若 NEXT blocked，跳到下一个非 blocked ready unit。",
        "4. 子任务结果返回后，逐个更新 unit 结论/状态，再决定下一轮是否继续 parallel_tasks。",
        "5. completed_units 中 needs_parent_verification=false 的证据视为可复用；不要重读相同 scope，除非相关文件已修改或 open_questions 明确要求。",
        "6. 最终汇总必须合并 completed_units 的所有结论；不要重做已完成线索。",
        "</working-memory-protocol>",
    ])
    return "\n".join(lines)


def _render_subagent_working_memory_block(session, memory: WorkingMemory) -> str:
    metadata = getattr(session, "metadata", {}) or {}
    lines = [
        (
            f"<working-memory task_id=\"{_xml_attr(memory.task_id)}\" "
            f"status=\"{_xml_attr(memory.status)}\" view=\"subagent_snapshot\">"
        ),
        f"子任务: {memory.objective or '(unknown)'}",
        f"父 session: {metadata.get('parent_session_id') or '(unknown)'}",
        f"最后检查点步骤: {memory.last_checkpoint_step}",
        "",
        "继承的已观察（不要重复这些调用，直接复用可用结论）:",
    ]
    lines.extend(_render_observed_calls(memory.observed_calls))

    lines.extend(["", "继承的已完成证据:"])
    if memory.completed_units:
        for unit in memory.completed_units[:8]:
            evidence = unit.get("evidence_refs") or []
            evidence_text = ", ".join(str(item) for item in evidence[:8])
            open_questions = unit.get("open_questions") or []
            verification = "yes" if unit.get("needs_parent_verification") else "no"
            lines.append(
                f"- [{unit.get('unit_id')}] {unit.get('description') or ''}\n"
                f"  结论: {unit.get('conclusion') or ''}\n"
                f"  证据: {evidence_text or '(none)'}\n"
                f"  需要父级复核: {verification}\n"
                f"  未解问题: {', '.join(str(item) for item in open_questions[:5]) or '(none)'}"
            )
    else:
        lines.append("- (none)")
    if memory.archived_findings:
        lines.extend(["", "继承的归档发现:"])
        for key, value in list(memory.archived_findings.items())[:8]:
            lines.append(f"- {key}: {_clip(json.dumps(value, ensure_ascii=False, default=str), 900)}")
    lines.extend([
        "</working-memory>",
        "",
        "<working-memory-protocol critical=\"true\" view=\"subagent_snapshot\">",
        "1. 这是父 agent working memory 的初始快照副本；父级 pending queue 不会继承，当前子任务以 active <subtask> prompt 为准。",
        "2. 优先复用 inherited completed evidence 和 observed calls，避免重复读取已覆盖 scope。",
        "3. 必须只按当前 prompt 指定的 scope 执行；不要启动/规划其他父级 pending units。",
        "4. 如果继承证据与当前文件观察冲突，以当前文件观察为准，并在最终 JSON 的 open_questions 或 needs_parent_verification 中说明。",
        "5. 最终只返回当前子任务的 findings/evidence/covered_scope/open_questions；父 agent 会负责合并。",
        "</working-memory-protocol>",
    ])
    return "\n".join(lines)


def _record_observed_calls(
    memory: WorkingMemory,
    *,
    tool_calls: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
    step: int,
) -> None:
    if not tool_calls and not tool_results:
        return
    for index, call in enumerate(tool_calls[:12]):
        if not isinstance(call, dict):
            continue
        tool_name = str(call.get("name") or "")
        if not tool_name:
            continue
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        result = _matching_tool_result(
            call,
            tool_results,
            index=index,
        )
        output = str((result or {}).get("output") or (result or {}).get("output_preview") or "")
        status = str((result or {}).get("status") or "")
        signature = tool_call_signature(tool_name, arguments)
        digest = tool_result_hash(output)
        previous = _observed_call_by_signature(memory, signature)
        count = _int((previous or {}).get("count"), 0) + 1
        info_gain = previous is None or digest != str(previous.get("result_hash") or "")
        entry = {
            "signature": signature,
            "tool": tool_name,
            "arguments": arguments,
            "label": _tool_call_label(tool_name, arguments),
            "gist": _observation_gist(tool_name, arguments, output, status),
            "step": _int(step, 0),
            "info_gain": bool(info_gain),
            "count": count,
            "result_hash": digest,
            "status": status,
            "updated_at": _now_iso(),
        }
        _upsert_observed_call(memory, entry)


def _matching_tool_result(
    call: dict[str, Any],
    results: list[dict[str, Any]],
    *,
    index: int,
) -> dict[str, Any]:
    if index < len(results) and isinstance(results[index], dict):
        candidate = results[index]
        if not candidate.get("name") or str(candidate.get("name") or "") == str(call.get("name") or ""):
            return candidate
    name = str(call.get("name") or "")
    for result in results or []:
        if isinstance(result, dict) and str(result.get("name") or "") == name:
            return result
    return {}


def _observed_call_by_signature(
    memory: WorkingMemory,
    signature: str,
) -> dict[str, Any] | None:
    for item in memory.observed_calls:
        if str(item.get("signature") or "") == signature:
            return item
    return None


def _upsert_observed_call(memory: WorkingMemory, entry: dict[str, Any]) -> None:
    signature = str(entry.get("signature") or "")
    if not signature:
        return
    kept = [
        item
        for item in memory.observed_calls
        if str(item.get("signature") or "") != signature
    ]
    kept.append(_normalize_observed_call(entry))
    kept.sort(key=lambda item: (_int(item.get("step"), 0), str(item.get("updated_at") or "")))
    memory.observed_calls = kept[-OBSERVED_CALLS_LIMIT:]


def _render_action_queue(units: list[dict[str, Any]]) -> list[str]:
    ordered = _ordered_pending_units(units)
    if not ordered:
        return ["- (none)"]
    lines: list[str] = []
    next_marked = False
    parallel_ids = _parallel_ready_unit_ids(ordered)
    for unit in ordered[:10]:
        blocked = _is_blocked(unit)
        marker = "  "
        if not next_marked and not blocked:
            marker = "→ NEXT "
            next_marked = True
        elif str(unit.get("unit_id") or "") in parallel_ids:
            marker = "↔ PARALLEL "
        scope = ", ".join(str(item) for item in (unit.get("scope_files") or [])[:6])
        blocked_by = unit.get("blocked_by") or []
        blocked_text = f" blocked_by={','.join(str(item) for item in blocked_by)}" if blocked_by else ""
        failure = unit.get("last_failure_reason") or ""
        failure_text = f" last_failure={_clip(failure, 120)}" if failure else ""
        scope_text = f" scope={scope}" if scope else ""
        lines.append(
            f"{marker}[{unit.get('priority')}] {unit.get('unit_id')}: "
            f"{_clip(unit.get('description') or '', 180)} "
            f"state={unit.get('state')}{scope_text}{blocked_text}{failure_text}".rstrip()
        )
    if len(ordered) > 10:
        lines.append(f"  ...[{len(ordered) - 10} more pending units omitted]")
    if parallel_ids:
        lines.append(
            f"  parallel_hint: {len(parallel_ids) + 1} independent ready units; "
            "prefer parallel_tasks before serial tool work."
        )
    return lines


def _parallel_ready_unit_ids(units: list[dict[str, Any]]) -> set[str]:
    ready = [unit for unit in units if not _is_blocked(unit)]
    if len(ready) < 2:
        return set()
    chosen_scopes: list[set[str]] = []
    parallel_ids: set[str] = set()
    for index, unit in enumerate(ready):
        scope = _unit_scope_set(unit)
        if not scope:
            continue
        if index == 0:
            chosen_scopes.append(scope)
            continue
        if any(scope & existing for existing in chosen_scopes):
            continue
        parallel_ids.add(str(unit.get("unit_id") or ""))
        chosen_scopes.append(scope)
    return parallel_ids


def _unit_scope_set(unit: dict[str, Any]) -> set[str]:
    return {
        str(item)
        for item in (unit.get("scope_files") or [])
        if str(item or "").strip()
    }


def _ordered_pending_units(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [_normalize_pending_unit(unit) for unit in units or []]
    return sorted(
        normalized,
        key=lambda unit: (
            _state_rank(unit.get("state")),
            _priority_rank(unit.get("priority")),
            1 if _is_blocked(unit) else 0,
            str(unit.get("unit_id") or ""),
        ),
    )


def _render_observed_calls(calls: list[dict[str, Any]]) -> list[str]:
    normalized = _normalize_observed_calls(calls)
    if not normalized:
        return ["- (none)"]
    lines = []
    for item in normalized[-12:]:
        count = _int(item.get("count"), 1)
        suffix = ""
        if count > 1:
            suffix += f" seen={count}"
        if item.get("info_gain") is False:
            suffix += " info_gain=false"
        lines.append(
            f"- {item.get('label') or item.get('tool')}: "
            f"{_clip(item.get('gist') or '', OBSERVED_GIST_CHARS)}{suffix}"
        )
    if len(normalized) > 12:
        lines.append(f"- ...[{len(normalized) - 12} older observations omitted]")
    return lines


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
    unit = _normalize_pending_unit(unit)
    unit_id = str(unit.get("unit_id") or "")
    for index, existing in enumerate(memory.pending_units):
        if str(existing.get("unit_id") or "") == unit_id:
            merged = dict(existing)
            merged.update({key: value for key, value in unit.items() if value not in (None, "")})
            memory.pending_units[index] = _normalize_pending_unit(merged)
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


def _normalize_pending_units(value: Any) -> list[dict[str, Any]]:
    return [
        _normalize_pending_unit(item)
        for item in _list_of_dicts(value)
    ]


def _normalize_pending_unit(unit: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(unit or {})
    normalized["unit_id"] = str(normalized.get("unit_id") or _stable_id(_task_description(normalized)))
    normalized["description"] = str(normalized.get("description") or "").strip()
    normalized["scope_files"] = [
        str(item)
        for item in (normalized.get("scope_files") or [])
        if str(item or "").strip()
    ]
    normalized["priority"] = _normalize_priority(normalized.get("priority"))
    normalized["blocked_by"] = _list_of_strings(normalized.get("blocked_by"))
    normalized["state"] = _normalize_state(
        normalized.get("state")
        or _pending_state(
            normalized.get("status"),
            failure_reason=normalized.get("last_failure_reason"),
        )
    )
    return normalized


def _normalize_observed_calls(value: Any) -> list[dict[str, Any]]:
    calls = [
        _normalize_observed_call(item)
        for item in _list_of_dicts(value)
    ]
    calls = [item for item in calls if item.get("signature")]
    calls.sort(key=lambda item: (_int(item.get("step"), 0), str(item.get("updated_at") or "")))
    return calls[-OBSERVED_CALLS_LIMIT:]


def _normalize_observed_call(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item or {})
    tool = str(normalized.get("tool") or "")
    arguments = normalized.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    signature = str(normalized.get("signature") or "")
    if not signature and tool:
        signature = tool_call_signature(tool, arguments)
    normalized["signature"] = signature
    normalized["tool"] = tool
    normalized["arguments"] = arguments
    normalized["label"] = str(normalized.get("label") or _tool_call_label(tool, arguments))
    normalized["gist"] = _clip(normalized.get("gist") or "", OBSERVED_GIST_CHARS)
    normalized["step"] = _int(normalized.get("step"), 0)
    normalized["info_gain"] = bool(normalized.get("info_gain", True))
    normalized["count"] = max(1, _int(normalized.get("count"), 1))
    normalized["result_hash"] = str(normalized.get("result_hash") or "")
    normalized["status"] = str(normalized.get("status") or "")
    normalized["updated_at"] = str(normalized.get("updated_at") or _now_iso())
    return normalized


def _upsert_step_checkpoint(memory: WorkingMemory, checkpoint: dict[str, Any]) -> None:
    step = _int(checkpoint.get("step"), 0)
    phase = str(checkpoint.get("phase") or "")
    for index, existing in enumerate(memory.step_checkpoints):
        if _int(existing.get("step"), 0) == step and str(existing.get("phase") or "") == phase:
            merged = dict(existing)
            merged.update(checkpoint)
            memory.step_checkpoints[index] = merged
            break
    else:
        memory.step_checkpoints.append(checkpoint)
    if len(memory.step_checkpoints) > STEP_CHECKPOINT_HISTORY_LIMIT:
        memory.step_checkpoints = memory.step_checkpoints[-STEP_CHECKPOINT_HISTORY_LIMIT:]


def _compact_tool_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted = []
    for call in calls[:12]:
        if not isinstance(call, dict):
            continue
        compacted.append({
            key: value
            for key, value in {
                "id": str(call.get("id") or ""),
                "name": str(call.get("name") or ""),
                "arguments_preview": _preview(call.get("arguments_preview", call.get("arguments"))),
            }.items()
            if value
        })
    return compacted


def _compact_tool_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted = []
    for result in results[:12]:
        if not isinstance(result, dict):
            continue
        compacted.append({
            key: value
            for key, value in {
                "name": str(result.get("name") or ""),
                "status": str(result.get("status") or ""),
                "output_preview": _preview(result.get("output")),
            }.items()
            if value
        })
    return compacted


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
            "evidence": result.get("evidence") or [],
            "covered_scope": _list_of_strings(result.get("covered_scope")),
            "open_questions": _list_of_strings(result.get("open_questions")),
            "needs_parent_verification": bool(result.get("needs_parent_verification")),
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
    for item in result.get("findings") or []:
        if isinstance(item, dict):
            path = item.get("path") or item.get("file") or item.get("source")
            if path:
                lines = str(item.get("lines") or "").strip()
                refs.append(f"{path}:{lines}" if lines else str(path))
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


def _task_priority(task: dict[str, Any]) -> str:
    return _normalize_priority(task.get("priority"))


def _task_blocked_by(task: dict[str, Any]) -> list[str]:
    for key in ("blocked_by", "blockedBy", "depends_on", "dependencies"):
        values = _list_of_strings(task.get(key))
        if values:
            return values
    return []


def _normalize_priority(value: Any) -> str:
    text = str(value or "P1").strip().upper()
    return text if text in PRIORITIES else "P1"


def _pending_state(value: Any, *, failure_reason: Any = "") -> str:
    if str(failure_reason or "").strip():
        return "blocked"
    text = str(value or "").strip().lower()
    if text in {"dispatched", "running", "in_progress", "started", "active"}:
        return "in_progress"
    if text in {"blocked", "failed", "error", "cancelled", "canceled", "stopped", "incomplete"}:
        return "blocked"
    return "todo"


def _normalize_state(value: Any) -> str:
    text = str(value or "todo").strip().lower()
    return text if text in PENDING_STATES else "todo"


def _state_rank(value: Any) -> int:
    state = _normalize_state(value)
    if state == "in_progress":
        return 0
    if state == "todo":
        return 1
    return 2


def _priority_rank(value: Any) -> int:
    priority = _normalize_priority(value)
    return PRIORITIES.index(priority)


def _is_blocked(unit: dict[str, Any]) -> bool:
    return _normalize_state(unit.get("state")) == "blocked" or bool(unit.get("blocked_by"))


def _list_of_strings(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (str, int, float)):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []
    return [str(item) for item in values if str(item or "").strip()]


def _tool_call_label(tool_name: str, arguments: dict[str, Any]) -> str:
    important_keys = {
        "path",
        "pattern",
        "query",
        "command",
        "offset",
        "limit",
        "glob",
        "recursive",
        "staged",
        "stat",
    }
    parts = []
    for key in sorted(arguments):
        if key not in important_keys:
            continue
        value = arguments.get(key)
        if value in (None, ""):
            continue
        rendered = repr(value) if isinstance(value, str) else str(value)
        parts.append(f"{key}={rendered}")
    if not parts and arguments:
        preview = _clip(json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str), 120)
        return f"{tool_name}({preview})"
    return f"{tool_name}({', '.join(parts)})"


def _observation_gist(
    tool_name: str,
    arguments: dict[str, Any],
    output: str,
    status: str,
) -> str:
    output = str(output or "")
    if status and status not in {"success", "ok"}:
        return _clip(f"{status}: {_first_meaningful_text(output)}", OBSERVED_GIST_CHARS)
    if tool_name in {"read_file", "read_files", "nl", "storage_read_file", "sandbox_read_file"}:
        symbols = _code_symbols_from_text(output)
        if symbols:
            return _clip("含 " + " / ".join(symbols[:8]), OBSERVED_GIST_CHARS)
    if tool_name in {"rg", "grep"}:
        return _clip(_search_gist(output), OBSERVED_GIST_CHARS)
    if tool_name in {"list_files", "storage_list_files", "sandbox_list_files"}:
        return _clip(_list_gist(output), OBSERVED_GIST_CHARS)
    if tool_name == "git_diff":
        return _clip(_diff_gist(output), OBSERVED_GIST_CHARS)
    if tool_name == "bash":
        return _clip(_bash_gist(arguments, output), OBSERVED_GIST_CHARS)
    return _clip(_first_meaningful_text(output), OBSERVED_GIST_CHARS)


def _code_symbols_from_text(text: str) -> list[str]:
    symbols = []
    patterns = [
        re.compile(r"^\s*(?:class|def)\s+([A-Za-z_]\w*)", re.MULTILINE),
        re.compile(r"^\s*async\s+def\s+([A-Za-z_]\w*)", re.MULTILINE),
        re.compile(r"^\s*(?:export\s+)?(?:class|function)\s+([A-Za-z_$][\w$]*)", re.MULTILINE),
    ]
    for pattern in patterns:
        for match in pattern.finditer(text or ""):
            symbols.append(match.group(1))
            if len(symbols) >= 8:
                return list(dict.fromkeys(symbols))
    return list(dict.fromkeys(symbols))


def _search_gist(output: str) -> str:
    files = []
    matches = 0
    for line in str(output or "").splitlines():
        match = re.match(r"^(?P<path>[^:\n]+):(?P<line>\d+)(?::\d+)?:", line)
        if not match:
            continue
        matches += 1
        files.append(match.group("path"))
    unique = list(dict.fromkeys(files))
    if unique:
        return f"命中 {matches} 行，涉及 {len(unique)} 个文件: {', '.join(unique[:5])}"
    return _first_meaningful_text(output)


def _list_gist(output: str) -> str:
    try:
        payload = json.loads(output)
    except Exception:
        return _first_meaningful_text(output)
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if isinstance(entries, list):
        files = sum(1 for item in entries if isinstance(item, dict) and item.get("type") == "file")
        dirs = sum(1 for item in entries if isinstance(item, dict) and item.get("type") == "dir")
        total = payload.get("total", len(entries))
        return f"{total} 项，当前页 files={files}, dirs={dirs}"
    return _first_meaningful_text(output)


def _diff_gist(output: str) -> str:
    files = []
    for line in str(output or "").splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                files.append(parts[3].removeprefix("b/"))
    if files:
        return f"修改 {len(files)} 个文件: {', '.join(files[:6])}"
    return _first_meaningful_text(output)


def _bash_gist(arguments: dict[str, Any], output: str) -> str:
    command = str(arguments.get("command") or "").strip()
    if "Traceback (most recent call last):" in output:
        tail = _last_meaningful_text(output)
        return f"{command}: traceback ends with {tail}" if command else f"traceback ends with {tail}"
    tail = _last_meaningful_text(output)
    return f"{command}: {tail}" if command else tail


def _first_meaningful_text(value: Any) -> str:
    for line in str(value or "").splitlines():
        text = line.strip()
        if text:
            return text
    return "(empty result)"


def _last_meaningful_text(value: Any) -> str:
    for line in reversed(str(value or "").splitlines()):
        text = line.strip()
        if text:
            return text
    return "(empty result)"


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


def _preview(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _clip(value, limit)
    return _clip(json.dumps(value, ensure_ascii=False, default=str), limit)


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
