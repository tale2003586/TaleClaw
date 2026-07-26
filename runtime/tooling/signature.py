"""Stable signatures for tool calls and results."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def tool_call_signature(tool_name: str, arguments: dict[str, Any] | None) -> str:
    return json.dumps(
        {
            "tool": str(tool_name or ""),
            "arguments": arguments or {},
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


def tool_result_hash(output: str) -> str:
    normalized = str(output or "")
    if normalized.startswith("[tool-cache] already read at step "):
        normalized = normalized.split("\n", 1)[1] if "\n" in normalized else ""
    normalized = normalized.strip()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
