#!/usr/bin/env python3
"""Run one TaleClaw agent task for the VS Code extension.

The extension invokes this script as a child process and reads the structured
result from --result-json. Stdout/stderr remain human-readable logs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    workspace = Path(args.workspace).expanduser().resolve()
    result_path = Path(args.result_json).expanduser().resolve()
    result_path.parent.mkdir(parents=True, exist_ok=True)

    if not workspace.exists() or not workspace.is_dir():
        result = {
            "status": "error",
            "error": f"Workspace does not exist or is not a directory: {workspace}",
            "workspace": str(workspace),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        write_json(result_path, result)
        print(result["error"], file=sys.stderr)
        return 2

    try:
        result = asyncio.run(run_task(args, workspace, started))
        write_json(result_path, result)
        return 0 if result.get("status") in {"completed", "stopped"} else 1
    except Exception as exc:  # pragma: no cover - exercised by extension users.
        result = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "workspace": str(workspace),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        write_json(result_path, result)
        print(result["error"], file=sys.stderr)
        return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one TaleClaw coding-agent task for VS Code.",
    )
    parser.add_argument("--workspace", required=True, help="Target coding workspace.")
    parser.add_argument("--task", required=True, help="User task text.")
    parser.add_argument(
        "--result-json",
        required=True,
        help="Path where the structured result JSON should be written.",
    )
    parser.add_argument(
        "--session-id",
        default="",
        help="Stable VS Code session id. Defaults to a generated id.",
    )
    parser.add_argument(
        "--mode",
        choices=["coding", "hybrid", "bot"],
        default="coding",
        help="Runtime mode to pin before running the task.",
    )
    parser.add_argument(
        "--max-reasoning-steps",
        type=int,
        default=24,
        help="Lead agent reasoning-step budget.",
    )
    parser.add_argument(
        "--subagent-max-reasoning-steps",
        type=int,
        default=16,
        help="Subagent reasoning-step budget.",
    )
    parser.add_argument("--rag-enabled", choices=["0", "1"], default="0")
    parser.add_argument("--context-budget-enabled", choices=["0", "1"], default="1")
    parser.add_argument("--working-memory-enabled", choices=["0", "1"], default="1")
    parser.add_argument("--tool-loop-guard-enabled", choices=["0", "1"], default="1")
    parser.add_argument("--user-id", default="vscode")
    parser.add_argument("--user-role", default="admin")
    return parser.parse_args()


async def run_task(args: argparse.Namespace, workspace: Path, started: float) -> dict[str, Any]:
    configure_env(args, workspace)

    # Import after configure_env so config.py sees workspace constants from the
    # extension. initialize_runtime_environment() then loads .env, so re-apply
    # per-task feature toggles before build_runtime reads dynamic env flags.
    from runtime import bootstrap

    bootstrap.initialize_runtime_environment()
    configure_env(args, workspace)
    runtime = bootstrap.build_runtime()
    apply_runtime_overrides(runtime, args)

    session_id = args.session_id.strip() or f"vscode-{uuid4().hex[:8]}"
    metadata = {
        "workspace_root": str(workspace),
        "user_id": args.user_id,
        "user_role": args.user_role,
        "client": "vscode-extension",
    }
    chunks: list[str] = []

    try:
        await pin_mode(runtime, args.mode, session_id, metadata)
        await runtime.run_message(
            content=args.task,
            channel="vscode",
            chat_id=session_id,
            sender="user",
            metadata=metadata,
            on_text=chunks.append,
        )
        session = runtime.loop.sessions.get_or_create(f"vscode:{session_id}")
        run_id = str(session.metadata.get("last_run_id") or "")
        run_dir = runtime.loop.trace_store.run_dir(run_id) if run_id else None
        run_state = read_json(run_dir / "run_state.json") if run_dir else {}
        metrics = read_json(run_dir / "metrics.json") if run_dir else {}
        report_payload = read_json(run_dir / "report.json") if run_dir else {}
        report = report_payload.get("report", {}) if isinstance(report_payload, dict) else {}
        task_session = report.get("metadata", {}).get("task_session", {})
        reply = str(run_state.get("final_answer") or "\n".join(chunks).strip())
        return {
            "status": str(run_state.get("status") or "completed"),
            "stop_reason": run_state.get("stop_reason"),
            "reply": reply,
            "workspace": str(workspace),
            "session_id": f"vscode:{session_id}",
            "run_id": run_id,
            "run_dir": str(run_dir) if run_dir else "",
            "trace_summary": str(run_dir / "trace_summary.md") if run_dir else "",
            "metrics": pick_metrics(metrics),
            "task_session": task_session,
            "workspace_diff": task_session.get("workspace_diff", {}),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "features": {
                "mode": args.mode,
                "rag_enabled": args.rag_enabled == "1",
                "context_budget_enabled": args.context_budget_enabled == "1",
                "working_memory_enabled": args.working_memory_enabled == "1",
                "tool_loop_guard_enabled": args.tool_loop_guard_enabled == "1",
                "max_reasoning_steps": args.max_reasoning_steps,
                "subagent_max_reasoning_steps": args.subagent_max_reasoning_steps,
            },
        }
    finally:
        await runtime.stop()


def configure_env(args: argparse.Namespace, workspace: Path) -> None:
    existing_roots = [
        item for item in os.getenv("WORKSPACE_ROOTS", "").split(os.pathsep) if item
    ]
    roots = [str(workspace), *existing_roots]
    os.environ["WORKSPACE_ROOTS"] = os.pathsep.join(dict.fromkeys(roots))
    os.environ["DEFAULT_CODING_WORKSPACE"] = str(workspace)
    os.environ["SUBAGENT_MAX_REASONING_STEPS"] = str(max(1, args.subagent_max_reasoning_steps))
    os.environ["RAG_ENABLED"] = args.rag_enabled
    os.environ["SECURITY_RAG_AUTO_CONTEXT_ENABLED"] = args.rag_enabled
    os.environ["SECURITY_RAG_PLUGIN_ENABLED"] = args.rag_enabled
    os.environ["HISTORY_VECTOR_ENABLED"] = args.rag_enabled
    os.environ["MEMORY_VECTOR_ENABLED"] = args.rag_enabled
    os.environ["CONTEXT_ENABLE_SECTION_BUDGET"] = args.context_budget_enabled
    os.environ["WORKING_MEMORY_CHECKPOINT_ENABLED"] = args.working_memory_enabled
    os.environ["WORKING_MEMORY_RESUME_ENABLED"] = args.working_memory_enabled
    os.environ["TOOL_LOOP_GUARD_ENABLED"] = args.tool_loop_guard_enabled


def apply_runtime_overrides(runtime: Any, args: argparse.Namespace) -> None:
    max_steps = max(1, int(args.max_reasoning_steps))
    pipeline = getattr(runtime.loop, "pipeline", None)
    agent_runner = getattr(pipeline, "agent_runner", None)
    if agent_runner is not None:
        agent_runner.max_reasoning_steps = max_steps
    task_runner = getattr(runtime.loop, "task_session_runner", None)
    base_pipeline = getattr(task_runner, "base_pipeline", None)
    base_agent_runner = getattr(base_pipeline, "agent_runner", None)
    if base_agent_runner is not None:
        base_agent_runner.max_reasoning_steps = max_steps
    subagent_runner = getattr(runtime.loop, "subagent_runner", None)
    if subagent_runner is not None:
        subagent_runner.max_reasoning_steps = max(1, int(args.subagent_max_reasoning_steps))


async def pin_mode(runtime: Any, mode: str, session_id: str, metadata: dict[str, Any]) -> None:
    command = {"coding": "/coding", "hybrid": "/hybrid", "bot": "/chat"}[mode]
    await runtime.run_message(
        content=command,
        channel="vscode",
        chat_id=session_id,
        sender="user",
        metadata=metadata,
    )


def pick_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "run_duration_ms",
        "reasoning_steps",
        "model_calls",
        "tool_calls",
        "tool_failures",
        "tool_denials",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "duplicate_tool_call_count",
        "duplicate_tool_call_ratio",
        "truncated_tool_output_count",
        "models",
        "tools",
    ]
    return {key: metrics.get(key) for key in keys if key in metrics}


def read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
