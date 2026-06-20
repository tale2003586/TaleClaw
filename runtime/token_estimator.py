from __future__ import annotations

import copy
import json
from typing import Any


DEFAULT_CONTEXT_LIMIT_TOKENS = 128000
DEFAULT_SAFE_CONTEXT_RATIO = 0.85


def estimate_tokens(messages: list[dict] | None, *, provider: Any | None = None) -> int:
    """Estimate prompt tokens for a model call.

    Providers may expose a better counter later. The fallback intentionally
    errs a little high for mixed Chinese/English text so the emergency trim
    fires before the API rejects the request.
    """
    items = list(messages or [])
    counter = getattr(provider, "count_tokens", None)
    if callable(counter):
        try:
            value = counter(items)
            if isinstance(value, dict):
                value = value.get("total_tokens") or value.get("input_tokens")
            if value is not None:
                return max(0, int(value))
        except Exception:
            pass
    return max(0, (sum(_message_chars(message) for message in items) + 2) // 3)


def context_limit(provider: Any | None = None, *, default: int = DEFAULT_CONTEXT_LIMIT_TOKENS) -> int:
    for attr in ("context_limit", "max_context_tokens", "max_input_tokens"):
        value = getattr(provider, attr, None)
        if value is None:
            continue
        try:
            parsed = int(value() if callable(value) else value)
        except Exception:
            continue
        if parsed > 0:
            return parsed
    return int(default)


def safe_context_limit(provider: Any | None = None, *, ratio: float = DEFAULT_SAFE_CONTEXT_RATIO) -> int:
    return max(1, int(context_limit(provider) * float(ratio)))


def emergency_trim(
    messages: list[dict],
    *,
    max_tokens: int,
    provider: Any | None = None,
) -> list[dict]:
    """Last-ditch prompt trim before a model call.

    Keeps the system message, the first two conversational groups, and the
    most recent groups. Groups are built around user turns and assistant/tool
    chains so we avoid creating invalid OpenAI-style tool-call ordering.
    """
    items = [copy.deepcopy(message) for message in (messages or []) if isinstance(message, dict)]
    if estimate_tokens(items, provider=provider) <= max_tokens:
        return items
    if not items:
        return []

    system_messages = [message for message in items if str(message.get("role") or "") == "system"]
    non_system = [message for message in items if str(message.get("role") or "") != "system"]
    groups = _conversation_groups(non_system)
    if not groups:
        return _trim_text_messages(system_messages, max_tokens=max_tokens, provider=provider)

    selected_indexes: set[int] = set()
    for index in range(min(2, len(groups))):
        selected_indexes.add(index)

    selected_messages = _flatten_groups(groups, selected_indexes, system_messages)
    tail_index = len(groups) - 1
    while tail_index >= 0 and estimate_tokens(selected_messages, provider=provider) <= max_tokens:
        selected_indexes.add(tail_index)
        selected_messages = _flatten_groups(groups, selected_indexes, system_messages)
        tail_index -= 1

    while estimate_tokens(selected_messages, provider=provider) > max_tokens and selected_indexes:
        removable = sorted(index for index in selected_indexes if index >= 2)
        if not removable:
            break
        selected_indexes.remove(removable[0])
        selected_messages = _flatten_groups(groups, selected_indexes, system_messages)

    if estimate_tokens(selected_messages, provider=provider) <= max_tokens:
        return selected_messages
    return _trim_text_messages(selected_messages, max_tokens=max_tokens, provider=provider)


def _conversation_groups(messages: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current: list[dict] = []
    for message in messages:
        role = str(message.get("role") or "")
        if role == "user" and current:
            groups.append(current)
            current = [message]
            continue
        current.append(message)
    if current:
        groups.append(current)
    return groups


def _flatten_groups(
    groups: list[list[dict]],
    selected_indexes: set[int],
    system_messages: list[dict],
) -> list[dict]:
    selected = list(system_messages)
    for index, group in enumerate(groups):
        if index in selected_indexes:
            selected.extend(group)
    return selected


def _trim_text_messages(
    messages: list[dict],
    *,
    max_tokens: int,
    provider: Any | None,
) -> list[dict]:
    trimmed = [copy.deepcopy(message) for message in messages]
    while estimate_tokens(trimmed, provider=provider) > max_tokens and trimmed:
        largest_index = max(
            range(1, len(trimmed)) if len(trimmed) > 1 else range(len(trimmed)),
            key=lambda index: _message_chars(trimmed[index]),
        )
        content = trimmed[largest_index].get("content")
        if not isinstance(content, str):
            trimmed.pop(largest_index)
            continue
        if len(content) < 200 and len(trimmed) > 1:
            trimmed.pop(largest_index)
            continue
        if len(content) < 200:
            keep_chars = max(1, int(len(content) * 0.5))
        else:
            keep_chars = max(120, int(len(content) * 0.65))
        if keep_chars >= len(content):
            keep_chars = max(1, len(content) - 1)
        trimmed[largest_index]["content"] = content[:keep_chars].rstrip() + "\n\n...[emergency trimmed]"
    return trimmed


def _message_chars(message: dict) -> int:
    try:
        return len(json.dumps(message, ensure_ascii=False, default=str))
    except TypeError:
        return len(str(message))
