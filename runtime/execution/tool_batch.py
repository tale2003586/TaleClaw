"""Pure argument and result projection for batched tool calls."""

from __future__ import annotations

import json


def task_call_arguments(call) -> dict:
    arguments = getattr(call, "arguments", None)
    if not isinstance(arguments, dict):
        arguments = {}
    task = {
        "prompt": str(arguments.get("prompt") or ""),
        "description": str(arguments.get("description") or ""),
        "agent_type": str(arguments.get("agent_type") or "explore"),
    }
    for key in ("scope", "objective", "deliverable", "budget"):
        value = arguments.get(key)
        if value not in (None, "", [], {}):
            task[key] = value
    return task


def read_file_call_arguments(call) -> dict:
    arguments = getattr(call, "arguments", None)
    if not isinstance(arguments, dict):
        arguments = {}
    item = {"path": str(arguments.get("path") or "")}
    for key in ("offset", "limit"):
        value = arguments.get(key)
        if value not in (None, "", [], {}):
            item[key] = value
    return item


def split_parallel_task_outputs(output: str, expected_count: int) -> list[str]:
    return _split_result_items(output, expected_count, preserve_item=True)


def split_read_files_outputs(output: str, expected_count: int) -> list[str]:
    return _split_result_items(output, expected_count, preserve_item=False)


def _split_result_items(
    output: str,
    expected_count: int,
    *,
    preserve_item: bool,
) -> list[str]:
    expected_count = max(0, int(expected_count))
    if expected_count <= 0:
        return []
    try:
        payload = json.loads(str(output or ""))
    except (TypeError, json.JSONDecodeError):
        return [str(output or "")] * expected_count
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return [str(output or "")] * expected_count

    results = payload["results"]
    split_outputs = []
    for index in range(expected_count):
        if index >= len(results):
            split_outputs.append(str(output or ""))
        elif preserve_item:
            split_outputs.append(
                json.dumps(results[index], ensure_ascii=False, indent=2, default=str)
            )
        elif isinstance(results[index], dict):
            split_outputs.append(str(results[index].get("output") or ""))
        else:
            split_outputs.append(str(output or ""))
    return split_outputs


__all__ = (
    "read_file_call_arguments",
    "split_parallel_task_outputs",
    "split_read_files_outputs",
    "task_call_arguments",
)
