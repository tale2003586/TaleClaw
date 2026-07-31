"""Compact, instruction-free prompt rendering for runtime task state."""

from __future__ import annotations

import json
from typing import Any

from .models import TaskStateCore


def render_task_state_core_message(state: TaskStateCore) -> dict[str, Any]:
    payload = state.core_dict()
    payload["completed"] = payload.get("completed", [])[-12:]
    payload["completion_basis"] = payload.get("completion_basis", [])[-8:]
    content = (
        '<task-state source="runtime-generated" instructions="false" '
        f'version="{state.version}">\n'
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        + "\n</task-state>"
    )
    return {
        "role": "system",
        "content": content,
        "metadata": {
            "kind": "task_state_context",
            "source": "runtime-generated",
            "instructions": False,
            "task_state_version": state.version,
        },
    }
