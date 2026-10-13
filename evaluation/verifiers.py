from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from evaluation.outcomes import verification_result


def verify_task(*, workspace: Path, run_dir: Path, task: Any) -> dict[str, Any]:
    """A broken grader produces an explicit unknown result, never a model fail."""
    try:
        return _verify_task(workspace=workspace, run_dir=run_dir, task=task)
    except Exception as exc:
        return {
            **verification_result("inconclusive", reason="grader_error"),
            "passed": None, "checks": [], "workspace_diff_summary": {},
            "error_type": type(exc).__name__, "error": str(exc),
        }


def _verify_task(
    *,
    workspace: Path,
    run_dir: Path,
    task: Any,
) -> dict[str, Any]:
    expected = dict(task.expected or {})
    checks = []

    command = str(expected.get("must_pass_command") or "").strip()
    if command:
        executed_command = _portable_python_command(command)
        result = subprocess.run(
            executed_command,
            cwd=workspace,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        checks.append({
            "name": "must_pass_command",
            "passed": result.returncode == 0,
            "command": command,
            "executed_command": executed_command,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
        })

    diff = _load_json(run_dir / "workspace_diff.json")
    if any(expected.get(name) for name in ("modified", "created", "not_modified")):
        _validate_workspace_diff(diff)
    modified = {
        item.get("path") if isinstance(item, dict) else str(item)
        for item in diff.get("modified", [])
    }
    created = set(str(path) for path in diff.get("created", []))
    deleted = set(str(path) for path in diff.get("deleted", []))
    touched = modified | created | deleted

    for path in expected.get("modified", []):
        checks.append({
            "name": "modified",
            "path": path,
            "passed": path in touched,
        })
    for path in expected.get("created", []):
        checks.append({
            "name": "created",
            "path": path,
            "passed": path in created,
        })
    for path in expected.get("not_modified", []):
        checks.append({
            "name": "not_modified",
            "path": path,
            "passed": path not in touched,
        })

    for item in expected.get("file_contains", []):
        path = workspace / item["path"]
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        checks.append({
            "name": "file_contains",
            "path": item["path"],
            "needle": item["text"],
            "passed": item["text"] in text,
        })

    trace_events = _trace_events(run_dir / "trace.jsonl")
    event_names = [event.get("event", "") for event in trace_events]
    for event_name in expected.get("trace_events", []):
        checks.append({
            "name": "trace_event_exists",
            "event": event_name,
            "passed": event_name in event_names,
        })

    tool_names = [
        (event.get("payload") or {}).get("tool_name") or (event.get("payload") or {}).get("name")
        for event in trace_events
        if event.get("event") in {"tool.call.completed", "tool.call.failed", "tool_executed"}
    ]
    for tool_name in expected.get("tool_called", []):
        checks.append({
            "name": "tool_called",
            "tool": tool_name,
            "passed": tool_name in tool_names,
        })

    for denied_tool in expected.get("tool_denied", []):
        checks.append({
            "name": "tool_denied",
            "tool": denied_tool,
            "passed": _tool_was_denied(trace_events, denied_tool),
        })

    run_state = _load_json(run_dir / "run_state.json")
    passed = all(check["passed"] for check in checks) if checks else None
    outcome = "not_run" if passed is None else "pass" if passed else "fail"
    return {
        **verification_result(outcome, reason="fixture_checks" if checks else "no_checks"),
        "passed": passed,
        "checks": checks,
        "workspace_diff_summary": diff.get("summary", {}),
        "run_status": run_state.get("status", ""),
    }


def _validate_workspace_diff(diff: dict[str, Any]) -> None:
    """An absent or incomplete artifact cannot prove that no file changed."""
    if not isinstance(diff, dict):
        raise ValueError("Workspace diff evidence must be an object.")
    for name in ("modified", "created", "deleted"):
        items = diff.get(name)
        if not isinstance(items, list):
            raise ValueError(f"Workspace diff evidence requires a {name} list.")
        for item in items:
            path = item.get("path") if name == "modified" and isinstance(item, dict) else item
            if not isinstance(path, str) or not path:
                raise ValueError(f"Workspace diff {name} evidence contains an invalid path.")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _portable_python_command(command: str) -> str:
    stripped = str(command or "").strip()
    if stripped == "python":
        return sys.executable
    if stripped.startswith("python "):
        return sys.executable + stripped[len("python"):]
    return stripped


def _trace_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def _tool_was_denied(events: list[dict[str, Any]], tool_name: str) -> bool:
    for event in events:
        payload = event.get("payload") or {}
        if payload.get("tool_name") != tool_name and payload.get("name") != tool_name:
            continue
        if payload.get("status") == "denied":
            return True
        output = str(payload.get("output_preview", "")).lower()
        if "denied" in output or "escapes workspace" in output:
            return True
        if "denied" in str(payload.get("error_type", "")).lower():
            return True
    return False
