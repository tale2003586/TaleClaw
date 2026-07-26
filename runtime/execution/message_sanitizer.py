"""Pure normalization of model context message sequences."""

from __future__ import annotations


def sanitize_context_messages(
    messages: list[dict],
) -> tuple[list[dict], list[dict]]:
    sanitized = []
    dropped = []
    items = list(messages or [])
    index = 0
    while index < len(items):
        message = items[index]
        if isinstance(message, dict) and is_empty_assistant_message(message):
            dropped.append({
                "index": index,
                "role": "assistant",
                "reason": "empty_assistant_message",
            })
            index += 1
            continue
        if isinstance(message, dict) and message.get("tool_calls"):
            replacement, next_index, drop_items = _sanitize_tool_call_group(items, index)
            if drop_items:
                dropped.extend(drop_items)
                sanitized.extend(replacement)
                index = next_index
                continue
            sanitized.extend(replacement)
            index = next_index
            continue
        if isinstance(message, dict) and str(message.get("role") or "") == "tool":
            dropped.append({
                "index": index,
                "role": "tool",
                "reason": "orphan_tool_result",
                "tool_call_id": message.get("tool_call_id"),
            })
            sanitized.append(_summarize_orphan_tool_result(message))
            index += 1
            continue
        sanitized.append(message)
        index += 1
    return sanitized, dropped


def is_empty_assistant_message(message: dict) -> bool:
    if str(message.get("role") or "") != "assistant":
        return False
    if message.get("tool_calls"):
        return False
    content = message.get("content")
    if content is None:
        return True
    if isinstance(content, str):
        return content == ""
    if isinstance(content, list):
        return len(content) == 0
    return False


def _sanitize_tool_call_group(
    messages: list[dict],
    index: int,
) -> tuple[list[dict], int, list[dict]]:
    message = messages[index]
    expected_ids = _tool_call_ids(message.get("tool_calls") or [])
    if not expected_ids:
        return [message], index + 1, []

    tool_messages = []
    cursor = index + 1
    seen_ids: set[str] = set()
    while cursor < len(messages):
        candidate = messages[cursor]
        if not isinstance(candidate, dict) or str(candidate.get("role") or "") != "tool":
            break
        tool_call_id = str(candidate.get("tool_call_id") or "")
        if tool_call_id not in expected_ids:
            break
        tool_messages.append(candidate)
        seen_ids.add(tool_call_id)
        cursor += 1
        if seen_ids >= expected_ids:
            break

    if seen_ids == expected_ids:
        return [message, *tool_messages], cursor, []

    replacement = _summarize_invalid_tool_call_group(
        message,
        tool_messages,
        expected_ids,
        seen_ids,
    )
    dropped = [{
        "index": index,
        "role": "assistant",
        "reason": "incomplete_tool_call_group",
        "expected_tool_call_ids": sorted(expected_ids),
        "seen_tool_call_ids": sorted(seen_ids),
    }]
    for offset, tool_message in enumerate(tool_messages, start=1):
        dropped.append({
            "index": index + offset,
            "role": "tool",
            "reason": "tool_result_belongs_to_incomplete_tool_call_group",
            "tool_call_id": tool_message.get("tool_call_id"),
        })
    return [replacement], cursor, dropped


def _summarize_invalid_tool_call_group(
    assistant_message: dict,
    tool_messages: list[dict],
    expected_ids: set[str],
    seen_ids: set[str],
) -> dict:
    tool_names = _tool_names_from_calls(assistant_message.get("tool_calls") or [])
    lines = [
        "[Context sanitizer note: an incomplete assistant tool-call group was converted to plain text before the model call.]",
        f"Expected tool_call_ids: {', '.join(sorted(expected_ids))}",
        f"Available tool_call_ids: {', '.join(sorted(seen_ids)) or '(none)'}",
    ]
    if tool_names:
        lines.append(f"Tool calls: {', '.join(tool_names)}")
    content = str(assistant_message.get("content") or "").strip()
    if content:
        lines.append(f"Assistant content: {content[:500]}")
    for tool_message in tool_messages:
        tool_text = str(tool_message.get("content") or "").replace("\n", " ")
        if len(tool_text) > 300:
            tool_text = tool_text[:297].rstrip() + "..."
        lines.append(f"Tool result {tool_message.get('tool_call_id')}: {tool_text}")
    return {"role": "user", "content": "\n".join(lines)}


def _summarize_orphan_tool_result(message: dict) -> dict:
    content = str(message.get("content") or "").replace("\n", " ")
    if len(content) > 500:
        content = content[:497].rstrip() + "..."
    return {
        "role": "user",
        "content": (
            "[Context sanitizer note: an orphan tool result was converted to plain text before the model call.]\n"
            f"tool_call_id={message.get('tool_call_id')}\n"
            f"{content}"
        ),
    }


def _tool_call_ids(tool_calls: list) -> set[str]:
    return {
        str(call.get("id"))
        for call in tool_calls or []
        if isinstance(call, dict) and call.get("id")
    }


def _tool_names_from_calls(tool_calls: list) -> list[str]:
    names = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        name = str(function.get("name") or call.get("name") or "").strip()
        if name:
            names.append(name)
    return names


__all__ = ("is_empty_assistant_message", "sanitize_context_messages")
