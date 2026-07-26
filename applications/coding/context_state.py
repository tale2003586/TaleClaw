from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

from runtime.token_estimator import estimate_tokens
from runtime.tooling.result_store import tool_result_ref_from_metadata
from runtime.working_memory import load_working_memory


CODING_CONTEXT_STATE_METADATA_KEY = "coding_context_state"
CODING_CONTEXT_STATE_VERSION = 1

DEFAULT_COMPACTION_TRIGGER_TOKENS = 12000
DEFAULT_COMPACTION_TARGET_TOKENS = 8000
DEFAULT_RECENT_GROUPS = 4

MAX_FINDINGS = 24
MAX_ACTIONS = 12
MAX_OBSERVATIONS = 36
MAX_DO_NOT_REPEAT = 36
MAX_EVIDENCE = 48


@dataclass
class MessageGroup:
    start: int
    end: int
    messages: list[dict[str, Any]]
    kind: str


@dataclass
class CodingContextState:
    version: int = CODING_CONTEXT_STATE_VERSION
    task_id: str = ""
    objective: str = ""
    workspace_root: str = ""
    active_turn_start_index: int = 0
    prompt_tail_start_index: int = 0
    compacted_until_index: int = 0
    source_cursor: int = 0
    generation: int = 0
    phase: str = "explore"
    finish_condition: str = ""
    coverage: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    pending_actions: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    do_not_repeat: list[dict[str, Any]] = field(default_factory=list)
    evidence_index: dict[str, dict[str, Any]] = field(default_factory=dict)
    observations: list[dict[str, Any]] = field(default_factory=list)
    last_compaction: dict[str, Any] | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @classmethod
    def from_payload(cls, payload: Any) -> "CodingContextState | None":
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return None
        if not isinstance(payload, dict):
            return None
        return cls(
            version=_int(payload.get("version"), CODING_CONTEXT_STATE_VERSION),
            task_id=str(payload.get("task_id") or ""),
            objective=str(payload.get("objective") or ""),
            workspace_root=str(payload.get("workspace_root") or ""),
            active_turn_start_index=_int(payload.get("active_turn_start_index"), 0),
            prompt_tail_start_index=_int(payload.get("prompt_tail_start_index"), 0),
            compacted_until_index=_int(payload.get("compacted_until_index"), 0),
            source_cursor=_int(payload.get("source_cursor"), 0),
            generation=_int(payload.get("generation"), 0),
            phase=str(payload.get("phase") or "explore"),
            finish_condition=str(payload.get("finish_condition") or ""),
            coverage=_list_of_dicts(payload.get("coverage")),
            findings=_list_of_dicts(payload.get("findings")),
            pending_actions=_list_of_dicts(payload.get("pending_actions")),
            open_questions=_list_of_strings(payload.get("open_questions")),
            do_not_repeat=_list_of_dicts(payload.get("do_not_repeat")),
            evidence_index=(
                dict(payload.get("evidence_index"))
                if isinstance(payload.get("evidence_index"), dict)
                else {}
            ),
            observations=_list_of_dicts(payload.get("observations")),
            last_compaction=(
                dict(payload.get("last_compaction"))
                if isinstance(payload.get("last_compaction"), dict)
                else None
            ),
            metrics=(
                dict(payload.get("metrics"))
                if isinstance(payload.get("metrics"), dict)
                else {}
            ),
            updated_at=str(payload.get("updated_at") or datetime.now(timezone.utc).isoformat()),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CodingContextView:
    state: CodingContextState
    state_message: dict[str, Any]
    recent_messages: list[dict[str, Any]]
    active_messages: list[dict[str, Any]]
    compacted: bool
    before_tokens: int
    after_tokens: int
    reduction: dict[str, Any] | None


def load_coding_context_state(session) -> CodingContextState | None:
    metadata = getattr(session, "metadata", {}) or {}
    return CodingContextState.from_payload(metadata.get(CODING_CONTEXT_STATE_METADATA_KEY))


def save_coding_context_state(session, state: CodingContextState) -> CodingContextState:
    state.updated_at = datetime.now(timezone.utc).isoformat()
    metadata = getattr(session, "metadata", None)
    if metadata is None:
        metadata = {}
        session.metadata = metadata
    metadata[CODING_CONTEXT_STATE_METADATA_KEY] = state.to_dict()
    touch = getattr(session, "touch", None)
    if callable(touch):
        touch()
    return state


def build_coding_context_view(
    session,
    *,
    objective: str,
    active_turn_start_index: int | None,
    static_messages: list[dict[str, Any]],
    threshold_tokens: int = DEFAULT_COMPACTION_TRIGGER_TOKENS,
    target_tokens: int = DEFAULT_COMPACTION_TARGET_TOKENS,
    keep_recent_groups: int = DEFAULT_RECENT_GROUPS,
) -> CodingContextView:
    messages = [dict(item) for item in (getattr(session, "messages", []) or [])]
    start = max(0, _int(active_turn_start_index, 0))
    state = _ensure_state(
        session,
        objective=objective,
        active_turn_start_index=start,
    )
    _sync_from_working_memory(session, state)

    groups = group_messages(messages, start_index=start)
    recent_messages = _messages_from_tail(groups, state.prompt_tail_start_index)
    state_message = render_coding_context_state_message(state)
    before_tokens = estimate_tokens([*static_messages, state_message, *recent_messages])
    compacted = False
    reduction = None

    threshold = max(1000, int(threshold_tokens or DEFAULT_COMPACTION_TRIGGER_TOKENS))
    target = max(500, int(target_tokens or DEFAULT_COMPACTION_TARGET_TOKENS))
    keep = max(1, int(keep_recent_groups or DEFAULT_RECENT_GROUPS))

    if before_tokens > threshold:
        compacted = _compact_until_under_target(
            state,
            groups,
            static_messages=static_messages,
            target_tokens=target,
            keep_recent_groups=keep,
        )
        if compacted:
            recent_messages = _messages_from_tail(groups, state.prompt_tail_start_index)
            state_message = render_coding_context_state_message(state)
            after_tokens = estimate_tokens([*static_messages, state_message, *recent_messages])
            reduction = {
                "section": "coding_context_state",
                "reason": "coding_prompt_state_compaction",
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
                "threshold_tokens": threshold,
                "target_tokens": target,
                "generation": state.generation,
                "compacted_until_index": state.compacted_until_index,
                "prompt_tail_start_index": state.prompt_tail_start_index,
            }
        else:
            after_tokens = before_tokens
    else:
        after_tokens = before_tokens

    state.metrics = {
        **dict(state.metrics or {}),
        "last_prompt_tokens": after_tokens,
        "last_recent_message_count": len(recent_messages),
        "last_active_message_count": len(messages[start:]),
    }
    save_coding_context_state(session, state)
    return CodingContextView(
        state=state,
        state_message=state_message,
        recent_messages=recent_messages,
        active_messages=messages[start:],
        compacted=compacted,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        reduction=reduction,
    )


def render_coding_context_state_message(state: CodingContextState) -> dict[str, Any]:
    payload = {
        "version": state.version,
        "task_id": state.task_id,
        "objective": state.objective,
        "phase": state.phase,
        "finish_condition": state.finish_condition,
        "generation": state.generation,
        "coverage": state.coverage[:12],
        "findings": state.findings[-MAX_FINDINGS:],
        "pending_actions": state.pending_actions[:MAX_ACTIONS],
        "open_questions": state.open_questions[:12],
        "do_not_repeat": state.do_not_repeat[-MAX_DO_NOT_REPEAT:],
        "observations": state.observations[-MAX_OBSERVATIONS:],
        "evidence_index": _limited_evidence_index(state.evidence_index),
        "last_compaction": state.last_compaction,
    }
    content = (
        '<coding-context-state critical="true">\n'
        + json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        + "\n</coding-context-state>\n\n"
        "<coding-context-protocol>\n"
        "1. Treat coding-context-state as the authoritative task state for this coding turn.\n"
        "2. Use recent raw messages only for details that have not yet been compacted into state.\n"
        "3. Do not repeat tools listed in do_not_repeat unless files changed or open_questions require it.\n"
        "4. If pending_actions contains a summarize/finalize action and evidence is sufficient, answer instead of exploring.\n"
        "5. Use retrieve_tool_result only when exact omitted evidence is needed.\n"
        "</coding-context-protocol>"
    )
    return {
        "role": "user",
        "content": content,
        "metadata": {
            "kind": "coding_context_state",
            "generation": state.generation,
        },
    }


def group_messages(messages: list[dict[str, Any]], *, start_index: int = 0) -> list[MessageGroup]:
    groups: list[MessageGroup] = []
    index = max(0, int(start_index or 0))
    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict):
            groups.append(MessageGroup(index, index + 1, [message], "unknown"))
            index += 1
            continue
        role = str(message.get("role") or "")
        if role == "assistant" and message.get("tool_calls"):
            expected = _tool_call_ids(message.get("tool_calls") or [])
            group = [message]
            seen: set[str] = set()
            cursor = index + 1
            while cursor < len(messages):
                candidate = messages[cursor]
                if not isinstance(candidate, dict) or str(candidate.get("role") or "") != "tool":
                    break
                call_id = str(candidate.get("tool_call_id") or "")
                if expected and call_id not in expected:
                    break
                group.append(candidate)
                seen.add(call_id)
                cursor += 1
                if expected and seen >= expected:
                    break
            groups.append(MessageGroup(index, cursor, group, "tool_group"))
            index = cursor
            continue
        groups.append(MessageGroup(index, index + 1, [message], role or "message"))
        index += 1
    return groups


def _ensure_state(
    session,
    *,
    objective: str,
    active_turn_start_index: int,
) -> CodingContextState:
    existing = load_coding_context_state(session)
    task_id = str(getattr(session, "id", "") or "")
    metadata = getattr(session, "metadata", {}) or {}
    workspace_root = str(metadata.get("workspace_root") or metadata.get("workspace_requested") or "")
    objective = str(objective or "").strip()
    if (
        existing is None
        or existing.version != CODING_CONTEXT_STATE_VERSION
        or existing.active_turn_start_index != active_turn_start_index
    ):
        return CodingContextState(
            task_id=task_id,
            objective=objective,
            workspace_root=workspace_root,
            active_turn_start_index=active_turn_start_index,
            prompt_tail_start_index=active_turn_start_index,
            compacted_until_index=active_turn_start_index,
            source_cursor=active_turn_start_index,
            finish_condition=_default_finish_condition(objective),
        )
    if objective and not existing.objective:
        existing.objective = objective
    if workspace_root and not existing.workspace_root:
        existing.workspace_root = workspace_root
    if not existing.finish_condition:
        existing.finish_condition = _default_finish_condition(existing.objective)
    return existing


def _sync_from_working_memory(session, state: CodingContextState) -> None:
    memory = load_working_memory(session)
    if memory is None:
        return
    if memory.objective and not state.objective:
        state.objective = memory.objective
    state.phase = _phase_from_memory(memory, state.phase)
    for unit in memory.completed_units or []:
        unit_id = str(unit.get("unit_id") or "")
        if not unit_id:
            continue
        finding = {
            "id": f"wm:{unit_id}",
            "claim": _clip(str(unit.get("conclusion") or ""), 900),
            "source": "working_memory.completed_units",
            "evidence": [str(item) for item in (unit.get("evidence_refs") or [])[:8]],
            "confidence": "medium" if unit.get("needs_parent_verification") else "high",
        }
        _upsert_by_id(state.findings, finding, limit=MAX_FINDINGS)
        _upsert_coverage(
            state,
            {
                "area": _clip(str(unit.get("description") or unit_id), 120),
                "status": "covered",
                "evidence": finding["evidence"],
            },
        )
    pending = []
    for unit in memory.pending_units or []:
        pending.append({
            "id": str(unit.get("unit_id") or _stable_id(unit)),
            "action": _clip(str(unit.get("description") or ""), 220),
            "priority": str(unit.get("priority") or "P1"),
            "state": str(unit.get("state") or "todo"),
            "scope": [str(item) for item in (unit.get("scope_files") or [])[:6]],
            "source": "working_memory.pending_units",
        })
    if pending:
        state.pending_actions = pending[:MAX_ACTIONS]
    for item in (memory.observed_calls or [])[-12:]:
        observation = {
            "id": _stable_id(item),
            "tool": str(item.get("tool") or ""),
            "scope": _clip(str(item.get("label") or ""), 180),
            "summary": _clip(str(item.get("gist") or ""), 220),
            "source": "working_memory.observed_calls",
        }
        _upsert_by_id(state.observations, observation, limit=MAX_OBSERVATIONS)
        if observation["tool"]:
            _upsert_by_id(
                state.do_not_repeat,
                {
                    "id": observation["id"],
                    "tool": observation["tool"],
                    "scope": observation["scope"],
                    "reason": "already observed in this coding turn",
                },
                limit=MAX_DO_NOT_REPEAT,
            )


def _compact_until_under_target(
    state: CodingContextState,
    groups: list[MessageGroup],
    *,
    static_messages: list[dict[str, Any]],
    target_tokens: int,
    keep_recent_groups: int,
) -> bool:
    tail_groups = [
        group
        for group in groups
        if group.start >= max(0, int(state.prompt_tail_start_index))
    ]
    if len(tail_groups) <= 1:
        return False

    keep = min(max(1, keep_recent_groups), len(tail_groups))
    compacted_any = False
    while keep >= 1:
        compact_groups = tail_groups[:-keep]
        kept_groups = tail_groups[-keep:]
        if not compact_groups or not kept_groups:
            break
        _absorb_groups_into_state(state, compact_groups)
        state.prompt_tail_start_index = kept_groups[0].start
        state.compacted_until_index = max(state.compacted_until_index, kept_groups[0].start)
        state.source_cursor = max(state.source_cursor, state.compacted_until_index)
        state.generation += 1
        state.last_compaction = {
            "generation": state.generation,
            "compacted_group_count": len(compact_groups),
            "kept_group_count": len(kept_groups),
            "compacted_until_index": state.compacted_until_index,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "deterministic_v1",
        }
        compacted_any = True

        rendered = [
            *static_messages,
            render_coding_context_state_message(state),
            *_flatten_groups(kept_groups),
        ]
        if estimate_tokens(rendered) <= target_tokens or keep <= 1:
            break
        keep -= 1
        tail_groups = kept_groups
    return compacted_any


def _absorb_groups_into_state(state: CodingContextState, groups: list[MessageGroup]) -> None:
    for group in groups:
        assistant = group.messages[0] if group.messages else {}
        first = group.messages[0] if group.messages else {}
        if isinstance(first, dict):
            role = str(first.get("role") or "")
            text = _message_content_text(first)
            if role == "user" and text:
                if not state.objective:
                    state.objective = _clip(text, 500)
                _upsert_by_id(
                    state.observations,
                    {
                        "id": f"msg:{group.start}:user",
                        "tool": "user_message",
                        "scope": "active_turn_request",
                        "summary": _clip(text, 320),
                    },
                    limit=MAX_OBSERVATIONS,
                )
            elif role == "assistant" and text:
                _upsert_by_id(
                    state.observations,
                    {
                        "id": f"msg:{group.start}:assistant",
                        "tool": "assistant_reasoning",
                        "scope": "compacted_assistant_message",
                        "summary": _clip(text, 320),
                    },
                    limit=MAX_OBSERVATIONS,
                )
        call_by_id = _assistant_call_map(assistant)
        for message in group.messages:
            if not isinstance(message, dict):
                continue
            if str(message.get("role") or "") != "tool":
                continue
            call_id = str(message.get("tool_call_id") or "")
            call = call_by_id.get(call_id) or {}
            tool_name = str(call.get("name") or message.get("name") or "tool")
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            content = str(message.get("content") or "")
            status = str(message.get("status") or "")
            label = _tool_label(tool_name, arguments)
            summary = _tool_summary(tool_name, arguments, content, status)
            evidence_id = _evidence_id(message, tool_name, arguments, content)
            ref = tool_result_ref_from_metadata(message.get("metadata"))
            evidence = {
                "id": evidence_id,
                "kind": "tool_result",
                "tool": tool_name,
                "scope": label,
                "summary": summary,
                "status": status or "unknown",
            }
            if ref:
                evidence["tool_result"] = ref.get("uri") or f"tool_result://{ref.get('result_id')}"
                evidence["chars"] = ref.get("chars")
                evidence["sha256"] = ref.get("sha256")
            path = _argument_path(arguments)
            if path:
                evidence["path"] = path
            state.evidence_index[evidence_id] = evidence
            _limit_mapping(state.evidence_index, MAX_EVIDENCE)
            _upsert_by_id(
                state.observations,
                {
                    "id": evidence_id,
                    "tool": tool_name,
                    "scope": label,
                    "summary": summary,
                    "evidence": evidence_id,
                },
                limit=MAX_OBSERVATIONS,
            )
            _upsert_by_id(
                state.do_not_repeat,
                {
                    "id": evidence_id,
                    "tool": tool_name,
                    "scope": label,
                    "reason": "compacted into coding_context_state",
                },
                limit=MAX_DO_NOT_REPEAT,
            )


def _messages_from_tail(groups: list[MessageGroup], start_index: int) -> list[dict[str, Any]]:
    selected = [group for group in groups if group.start >= max(0, int(start_index or 0))]
    return _flatten_groups(selected)


def _flatten_groups(groups: list[MessageGroup]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for group in groups:
        messages.extend(dict(message) if isinstance(message, dict) else message for message in group.messages)
    return messages


def _assistant_call_map(message: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        call_id = str(call.get("id") or "")
        if not call_id:
            continue
        args = function.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        result[call_id] = {
            "name": function.get("name") or call.get("name") or "",
            "arguments": args if isinstance(args, dict) else {},
        }
    return result


def _tool_call_ids(tool_calls: list[Any]) -> set[str]:
    ids = set()
    for call in tool_calls or []:
        if isinstance(call, dict) and call.get("id"):
            ids.add(str(call.get("id")))
    return ids


def _tool_label(tool_name: str, arguments: dict[str, Any]) -> str:
    keys = ("path", "pattern", "query", "command", "offset", "limit", "glob", "recursive")
    parts = []
    for key in keys:
        if key not in arguments:
            continue
        value = arguments.get(key)
        if value in (None, ""):
            continue
        parts.append(f"{key}={value!r}" if isinstance(value, str) else f"{key}={value}")
    return f"{tool_name}({', '.join(parts)})" if parts else tool_name


def _tool_summary(tool_name: str, arguments: dict[str, Any], content: str, status: str) -> str:
    if status and status not in {"success", "ok"}:
        return _clip(f"{status}: {_last_meaningful_text(content)}", 260)
    if tool_name in {"read_file", "read_files", "nl"}:
        symbols = _code_symbols_from_text(content)
        if symbols:
            return _clip("read code containing " + ", ".join(symbols[:8]), 260)
    if tool_name in {"rg", "grep"}:
        files = []
        matches = 0
        for line in str(content or "").splitlines():
            match = re.match(r"^(?P<path>[^:\n]+):(?P<line>\d+)(?::\d+)?:", line)
            if not match:
                continue
            matches += 1
            files.append(match.group("path"))
        unique = list(dict.fromkeys(files))
        if unique:
            return _clip(f"matched {matches} lines across {len(unique)} files: {', '.join(unique[:5])}", 260)
    if tool_name in {"list_files", "repo_map", "code_outline"}:
        return _clip(_first_meaningful_text(content), 260)
    if tool_name == "bash":
        return _clip(_last_meaningful_text(content), 260)
    return _clip(_first_meaningful_text(content), 260)


def _code_symbols_from_text(text: str) -> list[str]:
    patterns = [
        re.compile(r"^\s*(?:class|def)\s+([A-Za-z_]\w*)", re.MULTILINE),
        re.compile(r"^\s*async\s+def\s+([A-Za-z_]\w*)", re.MULTILINE),
        re.compile(r"^\s*(?:export\s+)?(?:class|function)\s+([A-Za-z_$][\w$]*)", re.MULTILINE),
    ]
    symbols = []
    for pattern in patterns:
        for match in pattern.finditer(text or ""):
            symbols.append(match.group(1))
            if len(symbols) >= 8:
                return list(dict.fromkeys(symbols))
    return list(dict.fromkeys(symbols))


def _evidence_id(message: dict[str, Any], tool_name: str, arguments: dict[str, Any], content: str) -> str:
    ref = tool_result_ref_from_metadata(message.get("metadata"))
    if ref and ref.get("result_id"):
        return f"ev:{ref.get('result_id')}"
    payload = json.dumps(
        {
            "tool": tool_name,
            "arguments": arguments,
            "hash": hashlib.sha1(str(content or "").encode("utf-8")).hexdigest()[:12],
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return f"ev:{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:12]}"


def _argument_path(arguments: dict[str, Any]) -> str:
    for key in ("path", "file"):
        value = arguments.get(key)
        if value:
            return str(value)
    files = arguments.get("files")
    if isinstance(files, list) and files:
        first = files[0]
        if isinstance(first, dict):
            return str(first.get("path") or first.get("file") or "")
        return str(first)
    return ""


def _upsert_by_id(items: list[dict[str, Any]], item: dict[str, Any], *, limit: int) -> None:
    item_id = str(item.get("id") or "")
    if not item_id:
        item["id"] = _stable_id(item)
        item_id = item["id"]
    for index, existing in enumerate(items):
        if str(existing.get("id") or "") == item_id:
            merged = {**existing, **item}
            items[index] = merged
            break
    else:
        items.append(item)
    if len(items) > limit:
        del items[: len(items) - limit]


def _upsert_coverage(state: CodingContextState, item: dict[str, Any]) -> None:
    area = str(item.get("area") or "")
    if not area:
        return
    for index, existing in enumerate(state.coverage):
        if str(existing.get("area") or "") == area:
            state.coverage[index] = {**existing, **item}
            return
    state.coverage.append(item)
    if len(state.coverage) > 12:
        state.coverage = state.coverage[-12:]


def _limit_mapping(value: dict[str, Any], limit: int) -> None:
    while len(value) > limit:
        first_key = next(iter(value))
        value.pop(first_key, None)


def _limited_evidence_index(value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    items = list((value or {}).items())[-MAX_EVIDENCE:]
    return {key: item for key, item in items}


def _phase_from_memory(memory, default: str) -> str:
    if getattr(memory, "status", "") == "completed":
        return "completed"
    if memory.pending_units:
        return "explore"
    if memory.completed_units:
        return "finalize"
    return default or "explore"


def _default_finish_condition(objective: str) -> str:
    objective = str(objective or "").strip()
    if not objective:
        return "Complete the current coding task and provide a concise final answer."
    return f"Answer the original coding request with enough evidence: {objective}"


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


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


def _stable_id(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:12]


def _message_content_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts).strip()
    return str(content or "").strip()


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


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    limit = max(1, int(limit or 1))
    if len(text) <= limit:
        return text
    return text[: limit - 16].rstrip() + "...[truncated]"


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
