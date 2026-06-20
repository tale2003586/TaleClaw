from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


BENCHMARK_SCHEMA_VERSION = 1
REQUIRED_TASK_KEYS = {
    "id",
    "category",
    "fixture_repo",
    "prompt",
    "step_budget",
    "allowed_tools",
    "expected",
    "script",
    "verifier",
}


@dataclass(frozen=True)
class BenchmarkTask:
    id: str
    category: str
    fixture_repo: str
    prompt: str
    step_budget: int
    allowed_tools: list[str]
    expected: dict[str, Any] = field(default_factory=dict)
    script: list[dict[str, Any]] = field(default_factory=list)
    verifier: str = ""


def load_benchmark(path: str | Path) -> list[BenchmarkTask]:
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Benchmark file must contain a JSON object.")
    if int(payload.get("schema_version", 0)) != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("Unsupported benchmark schema_version.")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Benchmark tasks must be a non-empty list.")

    seen = set()
    parsed = []
    for index, item in enumerate(tasks):
        if not isinstance(item, dict):
            raise ValueError(f"Task at index {index} must be an object.")
        missing = sorted(REQUIRED_TASK_KEYS - set(item))
        if missing:
            raise ValueError(f"Task {item.get('id', index)!r} is missing: {', '.join(missing)}")
        task_id = str(item["id"]).strip()
        if not task_id:
            raise ValueError(f"Task at index {index} has an empty id.")
        if task_id in seen:
            raise ValueError(f"Duplicate task id: {task_id}")
        seen.add(task_id)

        allowed_tools = item["allowed_tools"]
        if not isinstance(allowed_tools, list) or not allowed_tools:
            raise ValueError(f"Task {task_id} allowed_tools must be a non-empty list.")
        script = item["script"]
        if not isinstance(script, list) or not script:
            raise ValueError(f"Task {task_id} script must be a non-empty list.")

        parsed.append(BenchmarkTask(
            id=task_id,
            category=str(item["category"]).strip(),
            fixture_repo=str(item["fixture_repo"]).strip(),
            prompt=str(item["prompt"]).strip(),
            step_budget=max(1, int(item["step_budget"])),
            allowed_tools=[str(name).strip() for name in allowed_tools],
            expected=dict(item.get("expected") or {}),
            script=[dict(step) for step in script],
            verifier=str(item["verifier"]).strip(),
        ))
    return parsed
