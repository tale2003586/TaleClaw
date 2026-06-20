from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from runtime.context_budget import SectionBudgetRule


LATEST_RESULT_PROTECTED_TOOLS = {
    "read_file",
    "repo_map",
    "code_outline",
    "list_files",
}


@dataclass(frozen=True)
class BudgetedMessages:
    name: str
    raw_messages: list[dict[str, Any]]
    rendered_messages: list[dict[str, Any]]
    budget_chars: int | None = None
    floor_chars: int = 0
    strategy: str = "none"
    truncated: bool = False
    reduction: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def raw_chars(self) -> int:
        return messages_chars(self.raw_messages)

    @property
    def rendered_chars(self) -> int:
        return messages_chars(self.rendered_messages)


def budget_conversation_history(
    messages: list[dict[str, Any]],
    *,
    enabled: bool,
    rule: SectionBudgetRule | None,
) -> BudgetedMessages:
    name = "conversation_history"
    raw_messages = _copy_messages(messages)
    if rule is None:
        return BudgetedMessages(
            name=name,
            raw_messages=raw_messages,
            rendered_messages=_copy_messages(messages),
        )

    target = max(1, int(rule.budget_chars))
    floor = max(0, int(rule.floor_chars))
    metadata = {
        "budget_enabled": bool(enabled),
        "strategy": rule.strategy,
        "floor_chars": floor,
        "keep_head_turns": max(0, int(rule.keep_head_turns)),
        "keep_tail_turns": max(0, int(rule.keep_tail_turns)),
        "summary_chars": max(0, int(rule.summary_chars)),
        "transport": "chat_messages",
        "message_count": len(raw_messages),
    }

    if not enabled:
        return BudgetedMessages(
            name=name,
            raw_messages=raw_messages,
            rendered_messages=_copy_messages(messages),
            budget_chars=None,
            floor_chars=floor,
            strategy=rule.strategy,
            metadata=metadata,
        )

    raw_chars = messages_chars(raw_messages)
    if raw_chars <= target:
        return BudgetedMessages(
            name=name,
            raw_messages=raw_messages,
            rendered_messages=_copy_messages(messages),
            budget_chars=target,
            floor_chars=floor,
            strategy=rule.strategy,
            metadata=metadata,
        )

    rendered, summarized = _reduce_messages(raw_messages, rule)
    effective_target = max(target, min(floor, raw_chars))
    if messages_chars(rendered) > effective_target:
        rendered = _tail_trim_groups(rendered, effective_target)

    reduction = {
        "section": name,
        "reason": "section_budget",
        "before_chars": raw_chars,
        "after_chars": messages_chars(rendered),
        "budget_chars": target,
        "floor_chars": floor,
        "effective_budget_chars": effective_target,
        "strategy": rule.strategy,
        "before_messages": len(raw_messages),
        "after_messages": len(rendered),
        "summarized_messages": len(summarized),
    }
    metadata = {
        **metadata,
        "rendered_message_count": len(rendered),
        "summarized_message_count": len(summarized),
        "summarized_turn_count": len(_group_turns(summarized)),
    }
    return BudgetedMessages(
        name=name,
        raw_messages=raw_messages,
        rendered_messages=rendered,
        budget_chars=target,
        floor_chars=floor,
        strategy=rule.strategy,
        truncated=True,
        reduction=reduction,
        metadata=metadata,
    )


def budget_active_turn(
    messages: list[dict[str, Any]],
    *,
    enabled: bool,
    rule: SectionBudgetRule | None,
) -> BudgetedMessages:
    name = "active_turn"
    raw_messages = _copy_messages(messages)
    if rule is None:
        return BudgetedMessages(
            name=name,
            raw_messages=raw_messages,
            rendered_messages=_copy_messages(messages),
        )

    target = max(1, int(rule.budget_chars))
    floor = max(0, int(rule.floor_chars))
    metadata = {
        "budget_enabled": bool(enabled),
        "strategy": rule.strategy,
        "floor_chars": floor,
        "summary_chars": max(0, int(rule.summary_chars)),
        "keep_recent_results": max(0, int(rule.keep_recent_results)),
        "preserve_tools": list(rule.preserve_tools),
        "transport": "chat_messages",
        "message_count": len(raw_messages),
        "preserve": True,
    }

    if not enabled:
        return BudgetedMessages(
            name=name,
            raw_messages=raw_messages,
            rendered_messages=_copy_messages(messages),
            budget_chars=None,
            floor_chars=floor,
            strategy=rule.strategy,
            metadata=metadata,
        )

    working_messages, compressed_tool_results = _compress_old_tool_results(
        raw_messages,
        rule,
    )
    raw_chars = messages_chars(raw_messages)
    if messages_chars(working_messages) <= target:
        reduction = None
        if compressed_tool_results:
            reduction = {
                "section": name,
                "reason": "old_tool_result_compression",
                "before_chars": raw_chars,
                "after_chars": messages_chars(working_messages),
                "budget_chars": target,
                "floor_chars": floor,
                "strategy": "replace_old_tool_results",
                "compressed_tool_results": compressed_tool_results,
            }
        return BudgetedMessages(
            name=name,
            raw_messages=raw_messages,
            rendered_messages=working_messages,
            budget_chars=target,
            floor_chars=floor,
            strategy=rule.strategy,
            truncated=bool(compressed_tool_results),
            reduction=reduction,
            metadata={
                **metadata,
                "compressed_tool_results": compressed_tool_results,
                "rendered_message_count": len(working_messages),
            },
        )

    rendered, summarized = _reduce_active_turn(working_messages, rule)
    effective_target = max(target, min(floor, raw_chars))
    if messages_chars(rendered) > effective_target:
        rendered = _compact_active_tail(rendered, effective_target)

    reduction = {
        "section": name,
        "reason": "section_budget",
        "before_chars": raw_chars,
        "after_chars": messages_chars(rendered),
        "budget_chars": target,
        "floor_chars": floor,
        "effective_budget_chars": effective_target,
        "strategy": rule.strategy,
        "before_messages": len(raw_messages),
        "after_messages": len(rendered),
        "summarized_messages": len(summarized),
        "compressed_tool_results": compressed_tool_results,
    }
    metadata = {
        **metadata,
        "rendered_message_count": len(rendered),
        "summarized_message_count": len(summarized),
        "latest_tool_call_preserved": _last_tool_call_index(rendered) is not None,
        "compressed_tool_results": compressed_tool_results,
    }
    return BudgetedMessages(
        name=name,
        raw_messages=raw_messages,
        rendered_messages=rendered,
        budget_chars=target,
        floor_chars=floor,
        strategy=rule.strategy,
        truncated=True,
        reduction=reduction,
        metadata=metadata,
    )


def messages_chars(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages or []:
        if not isinstance(message, dict):
            total += len(str(message))
            continue
        total += len(str(message.get("role") or ""))
        total += len(str(message.get("content") or ""))
        for key in ("tool_calls", "tool_call_id", "name", "status"):
            if key in message:
                total += len(json.dumps(message.get(key), ensure_ascii=False, default=str))
    return total


def _reduce_messages(
    messages: list[dict[str, Any]],
    rule: SectionBudgetRule,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    strategy = str(rule.strategy or "summary_middle")
    groups = _group_turns(messages)
    if not groups:
        return [], []

    head_count = max(0, int(rule.keep_head_turns))
    tail_count = max(0, int(rule.keep_tail_turns))
    if strategy == "tail_messages":
        head_count = 0
    elif strategy == "head_tail_messages":
        pass
    elif strategy != "summary_middle":
        head_count = 0

    if head_count + tail_count >= len(groups):
        tail_count = max(0, len(groups) - head_count)

    head = groups[:head_count]
    tail = groups[len(groups) - tail_count :] if tail_count else []
    middle_start = len(head)
    middle_end = len(groups) - len(tail)
    middle = groups[middle_start:middle_end]

    rendered = _flatten(head)
    summarized = _flatten(middle)
    if strategy == "summary_middle" and summarized:
        rendered.append(_summary_message(summarized, rule.summary_chars))
    rendered.extend(_flatten(tail))
    return rendered, summarized


def _tail_trim_groups(messages: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    groups = _group_turns(messages)
    kept: list[list[dict[str, Any]]] = []
    total = 0
    marker = [{
        "role": "user",
        "content": "[Context budget note: older conversation history was omitted after section budgeting.]",
    }]
    marker_chars = messages_chars(marker)
    for group in reversed(groups):
        group_chars = messages_chars(group)
        if kept and total + group_chars + marker_chars > limit:
            break
        if not kept and group_chars + marker_chars > limit:
            kept.append(_compact_group(group, max(1, limit - marker_chars)))
            break
        kept.append(group)
        total += group_chars
    kept.reverse()
    return marker + _flatten(kept)


def _compact_group(group: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    summary = _summary_message(group, limit)
    return [summary]


def _summary_message(messages: list[dict[str, Any]], max_chars: int) -> dict[str, str]:
    max_chars = int(max_chars or 1200)
    if max_chars <= 0:
        max_chars = 1200
    groups = _group_turns(messages)
    lines = [
        "[Conversation history summary: middle turns were compressed by ContextBuilder.]",
        f"Compressed turns: {len(groups)}.",
        f"Compressed messages: {len(messages)}.",
    ]
    for turn_index, group in enumerate(groups, 1):
        tool_names = _tool_names_by_id(group)
        user_text = ""
        for message in group:
            if str(message.get("role") or "") == "user":
                user_text = _squash(_message_text(message), 220)
                break
        if user_text:
            lines.append(f"- Turn {turn_index}: user asked: {user_text}")
        else:
            lines.append(f"- Turn {turn_index}:")

        for message in group:
            role = str(message.get("role") or "unknown")
            if role == "user":
                continue
            if role == "assistant":
                calls = _tool_call_names(message.get("tool_calls") or [])
                if calls:
                    lines.append(f"    -> called tools: {calls}")
                content = _squash(_message_text(message), 160)
                if content:
                    lines.append(f"    -> assistant said: {content}")
            elif role == "tool":
                tool_id = str(message.get("tool_call_id") or "")
                tool_name = tool_names.get(tool_id, "unknown_tool")
                status = str(message.get("status") or "")
                content = _squash(_message_text(message), 180)
                lines.append(f"    -> [{tool_id}] {tool_name} {status}: {content}")
            else:
                content = _squash(_message_text(message), 160)
                if content:
                    lines.append(f"    -> {role}: {content}")
        if len("\n".join(lines)) >= max_chars:
            lines.append("...[summary truncated]")
            break
    content = "\n".join(lines)
    if len(content) > max_chars:
        marker = "\n...[summary truncated]"
        if max_chars <= len(marker) + 8:
            content = content[:max_chars]
        else:
            content = content[: max_chars - len(marker)].rstrip() + marker
    return {"role": "user", "content": content}


def _compress_old_tool_results(
    messages: list[dict[str, Any]],
    rule: SectionBudgetRule,
) -> tuple[list[dict[str, Any]], int]:
    rendered = _copy_messages(messages)
    tool_names = _tool_names_by_id(rendered)
    keep_recent = max(0, int(rule.keep_recent_results))
    preserve_tools = {str(item) for item in rule.preserve_tools}

    tool_indexes = [
        index
        for index, message in enumerate(rendered)
        if isinstance(message, dict) and str(message.get("role") or "") == "tool"
    ]
    recent_keep = set(tool_indexes[-keep_recent:]) if keep_recent else set()
    latest_protected = _latest_tool_result_indexes(
        rendered,
        tool_indexes,
        tool_names,
        LATEST_RESULT_PROTECTED_TOOLS,
    )
    compressed = 0
    for index in tool_indexes:
        message = rendered[index]
        tool_id = str(message.get("tool_call_id") or "")
        tool_name = tool_names.get(tool_id, "unknown_tool")
        if index in recent_keep or index in latest_protected or tool_name in preserve_tools:
            continue
        content = _message_text(message)
        placeholder = _compressed_tool_placeholder(tool_name, message)
        if content == placeholder:
            continue
        message["content"] = placeholder
        message["metadata"] = {
            **(message.get("metadata") if isinstance(message.get("metadata"), dict) else {}),
            "compressed_by": "context_budget",
            "original_chars": len(content),
        }
        compressed += 1
    return rendered, compressed


def _latest_tool_result_indexes(
    messages: list[dict[str, Any]],
    indexes: list[int],
    tool_names: dict[str, str],
    protected_tools: set[str],
) -> set[int]:
    latest: dict[str, int] = {}
    for index in indexes:
        message = messages[index]
        tool_id = str(message.get("tool_call_id") or "")
        tool_name = tool_names.get(tool_id, "unknown_tool")
        if tool_name in protected_tools:
            latest[tool_name] = index
    return set(latest.values())


def _compressed_tool_placeholder(tool_name: str, message: dict[str, Any]) -> str:
    if tool_name in {"read_file", "storage_read_file", "sandbox_read_file"}:
        args = message.get("final_arguments") if isinstance(message.get("final_arguments"), dict) else {}
        path = str(args.get("path") or "")
        offset = args.get("offset", 0)
        limit = args.get("limit")
        if path:
            limit_part = f", limit={limit}" if limit not in (None, "") else ""
            return (
                f"<{tool_name} result compressed for context budget; "
                f"path={path}; offset={offset}{limit_part}; "
                f"re-read with {tool_name}(path=\"{path}\", offset={offset}{limit_part})>"
            )
    if tool_name in {"list_files", "storage_list_files", "sandbox_list_files"}:
        args = message.get("final_arguments") if isinstance(message.get("final_arguments"), dict) else {}
        path = str(args.get("path") or ".")
        offset = args.get("offset", 0)
        recursive = args.get("recursive")
        if path:
            recursive_part = f", recursive={bool(recursive)}" if recursive is not None else ""
            return (
                f"<{tool_name} result compressed for context budget; "
                f"path={path}; offset={offset}; "
                f"re-list with {tool_name}(path=\"{path}\"{recursive_part}, offset={offset})>"
            )
    if tool_name == "repo_map":
        args = message.get("final_arguments") if isinstance(message.get("final_arguments"), dict) else {}
        path = str(args.get("path") or ".")
        offset = args.get("offset", 0)
        max_depth = args.get("max_depth")
        include_lines = args.get("include_lines")
        if path:
            max_depth_part = f", max_depth={max_depth}" if max_depth not in (None, "") else ""
            include_part = (
                f", include_lines={bool(include_lines)}"
                if include_lines is not None
                else ""
            )
            return (
                f"<repo_map result compressed for context budget; "
                f"path={path}; offset={offset}{max_depth_part}{include_part}; "
                f"re-map with repo_map(path=\"{path}\"{max_depth_part}{include_part}, offset={offset})>"
            )
    if tool_name == "code_outline":
        args = message.get("final_arguments") if isinstance(message.get("final_arguments"), dict) else {}
        path = str(args.get("path") or "")
        offset = args.get("offset", 0)
        limit = args.get("limit")
        if path:
            limit_part = f", limit={limit}" if limit not in (None, "") else ""
            return (
                f"<code_outline result compressed for context budget; "
                f"path={path}; offset={offset}{limit_part}; "
                f"re-outline with code_outline(path=\"{path}\", offset={offset}{limit_part})>"
            )
    return f"<{tool_name} result compressed for context budget>"


def _group_turns(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages or []:
        role = str(message.get("role") or "")
        if role == "user" and current:
            groups.append(current)
            current = []
        current.append(dict(message))
    if current:
        groups.append(current)
    return groups


def _flatten(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [dict(message) for group in groups for message in group]


def _copy_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(message) for message in messages or []]


def _reduce_active_turn(
    messages: list[dict[str, Any]],
    rule: SectionBudgetRule,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not messages:
        return [], []

    first_user_index = _first_role_index(messages, "user")
    if first_user_index is None:
        first_user_index = 0

    user_prefix = _copy_messages(messages[: first_user_index + 1])
    latest_tool_start = _last_tool_call_index(messages)
    if latest_tool_start is None or latest_tool_start <= first_user_index:
        rendered = _copy_messages(user_prefix)
        summarized = _copy_messages(messages[first_user_index + 1 :])
        if summarized:
            rendered.append(_active_turn_summary_message(summarized, rule.summary_chars))
        return rendered, summarized

    latest_tool_end = _tool_group_end(messages, latest_tool_start)
    latest_group = _copy_messages(messages[latest_tool_start:latest_tool_end])
    trailing = _copy_messages(messages[latest_tool_end:])
    summarized = _copy_messages(messages[first_user_index + 1 : latest_tool_start])

    rendered = _copy_messages(user_prefix)
    if summarized:
        rendered.append(_active_turn_summary_message(summarized, rule.summary_chars))
    rendered.extend(latest_group)
    rendered.extend(trailing)
    return rendered, summarized


def _compact_active_tail(messages: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if messages_chars(messages) <= limit:
        return _copy_messages(messages)

    first_user_index = _first_role_index(messages, "user")
    if first_user_index is None:
        first_user_index = 0
    latest_tool_start = _last_tool_call_index(messages)
    if latest_tool_start is None:
        return _tail_trim_groups(messages, limit)

    prefix = _copy_messages(messages[: first_user_index + 1])
    summary_candidates = _copy_messages(messages[first_user_index + 1 : latest_tool_start])
    tool_group = _copy_messages(messages[latest_tool_start:])
    rendered = prefix
    if summary_candidates:
        summary_limit = max(120, min(limit // 3, limit - messages_chars(prefix)))
        rendered.append(_active_turn_summary_message(summary_candidates, summary_limit))
    rendered.extend(_compact_tool_group_if_needed(tool_group, max(200, limit - messages_chars(rendered))))
    if messages_chars(rendered) > limit:
        rendered = _force_trim_tool_contents(rendered, limit)
    return rendered


def _compact_tool_group_if_needed(
    group: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    if messages_chars(group) <= limit:
        return _copy_messages(group)
    rendered: list[dict[str, Any]] = []
    remaining = max(200, limit)
    for message in group:
        copied = dict(message)
        if str(copied.get("role") or "") == "tool":
            content = _message_text(copied)
            keep = max(120, remaining // max(1, len(group)))
            if len(content) > keep:
                copied["content"] = (
                    content[:keep].rstrip()
                    + "\n...[active turn tool result truncated by context budget]"
                )
        rendered.append(copied)
    return rendered


def _force_trim_tool_contents(
    messages: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    rendered = _copy_messages(messages)
    marker = "\n...[active turn tool result truncated by context budget]"
    for index in range(len(rendered) - 1, -1, -1):
        if messages_chars(rendered) <= limit:
            break
        message = rendered[index]
        if str(message.get("role") or "") != "tool":
            continue
        content = _message_text(message)
        overflow = messages_chars(rendered) - limit
        keep = max(80, len(content) - overflow - len(marker))
        if keep < len(content):
            message["content"] = content[:keep].rstrip() + marker
    return rendered


def _active_turn_summary_message(
    messages: list[dict[str, Any]],
    max_chars: int,
) -> dict[str, str]:
    summary = _summary_message(messages, max_chars)
    summary["content"] = summary["content"].replace(
        "[Conversation history summary: middle turns were compressed by ContextBuilder.]",
        "[Active turn summary: earlier tool calls/results were compressed by ContextBuilder.]",
        1,
    )
    return summary


def _first_role_index(messages: list[dict[str, Any]], role: str) -> int | None:
    for index, message in enumerate(messages or []):
        if isinstance(message, dict) and str(message.get("role") or "") == role:
            return index
    return None


def _last_tool_call_index(messages: list[dict[str, Any]]) -> int | None:
    for index in range(len(messages or []) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("tool_calls"):
            return index
    return None


def _tool_group_end(messages: list[dict[str, Any]], tool_call_index: int) -> int:
    ids = _tool_call_ids(messages[tool_call_index].get("tool_calls") or [])
    index = tool_call_index + 1
    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict):
            break
        role = str(message.get("role") or "")
        if role != "tool":
            break
        if ids and str(message.get("tool_call_id") or "") not in ids:
            break
        index += 1
    return index


def _tool_call_ids(tool_calls: list[Any]) -> set[str]:
    ids: set[str] = set()
    for call in tool_calls:
        if isinstance(call, dict) and call.get("id"):
            ids.add(str(call.get("id")))
    return ids


def _tool_names_by_id(messages: list[dict[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            if not call_id:
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            names[call_id] = str(function.get("name") or call.get("name") or "unknown_tool")
    return names


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content or "")


def _tool_call_names(tool_calls: list[Any]) -> str:
    names = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        names.append(str(function.get("name") or call.get("name") or "unknown_tool"))
    return ",".join(names)


def _squash(text: str, limit: int) -> str:
    value = str(text or "").strip().replace("\n", " ")
    if len(value) > limit:
        return value[: max(0, limit - 3)].rstrip() + "..."
    return value
