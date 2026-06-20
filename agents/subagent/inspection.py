from __future__ import annotations

import re
from typing import Any


def count_tool_calls(messages: list[dict[str, Any]]) -> int:
    count = 0
    for message in messages:
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            count += len(calls)
    return count


def extract_files_touched(messages: list[dict[str, Any]]) -> list[str]:
    paths: set[str] = set()
    for message in messages:
        if message.get("role") != "tool":
            continue
        args = message.get("final_arguments")
        if isinstance(args, dict):
            _collect_paths(args, paths)
        content = str(message.get("content") or "")
        for match in re.findall(r"(?:Wrote|Edited)\s+(?:\d+\s+bytes\s+to\s+)?([^\n]+)", content):
            cleaned = match.strip()
            if cleaned and not cleaned.startswith("Error:"):
                paths.add(cleaned)
    return sorted(paths)


def _collect_paths(value: Any, paths: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"path", "file", "filename"} and isinstance(item, str):
                paths.add(item)
            elif key == "paths" and isinstance(item, list):
                for path in item:
                    if isinstance(path, str):
                        paths.add(path)
            else:
                _collect_paths(item, paths)
    elif isinstance(value, list):
        for item in value:
            _collect_paths(item, paths)
