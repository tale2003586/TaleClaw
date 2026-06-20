from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from agents.coding.runner import TaskSessionRunner
from evaluation.metrics import failure_category, summarize_rows
from evaluation.task_schema import BenchmarkTask, load_benchmark
from evaluation.verifiers import verify_task
from memory.store import MemoryStore
from models.provider import LLMResponse, ToolCall
from modes.coding import CODING_PROFILE
from plugins import PluginManager
from plugins.eval_report import EvalReportPlugin
from runtime.context import ContextBuilder
from runtime.pipeline import Pipeline
from runtime.trace.run_state import RunState
from runtime.trace.trace_store import TraceStore
from runtime.workspace import WorkspaceResolver
from sessions.session import Session, SessionManager
from tools.executor import ToolExecutor
from tools.handlers import cleanup_expired_sandboxes
from tools.hooks import (
    FileWriteScopeHook,
    ShellSafetyHook,
    ShellWorkspaceScopeHook,
    ToolLoopGuardHook,
    ToolTraceHook,
)
from tools.tool_registry import build_lead_tool_registry


DEFAULT_BENCHMARK_PATH = Path("benchmarks/coding_tasks.json")
DEFAULT_EVAL_ROOT = Path(".evals/runs")
DEFAULT_UNBOUNDED_EVAL_MAX_REASONING_STEPS = 1000
RUNNER_MODES = {"scripted", "real"}


class ScriptedProvider:
    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self.steps = list(steps)
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        if kwargs.get("tool_choice") == "none" or not kwargs.get("tools"):
            return LLMResponse(
                content=json.dumps({"summary": "benchmark task completed", "conclusions": []}),
                raw_message={"role": "assistant", "content": "{}"},
            )
        if not self.steps:
            return _final_response("Done.")
        step = self.steps.pop(0)
        if "tool" in step:
            return _tool_response(
                len(self.calls),
                str(step["tool"]),
                dict(step.get("args") or {}),
            )
        return _final_response(str(step.get("final", "Done.")))


class CodingBenchmarkHarness:
    def __init__(
        self,
        *,
        benchmark_path: str | Path = DEFAULT_BENCHMARK_PATH,
        eval_root: str | Path = DEFAULT_EVAL_ROOT,
        workspace_root: str | Path | None = None,
        runner_mode: str = "scripted",
        task_id: str | None = None,
        keep_workspace: bool = False,
        no_step_budget: bool = False,
        max_reasoning_steps: int | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
        plugin_manager: PluginManager | None = None,
    ) -> None:
        self.benchmark_path = Path(benchmark_path)
        self.repo_root = self.benchmark_path.resolve().parent.parent
        self.eval_root = Path(eval_root)
        self.workspace_root = Path(workspace_root) if workspace_root is not None else None
        self.runner_mode = runner_mode
        if self.runner_mode not in RUNNER_MODES:
            raise ValueError(f"Unsupported runner_mode: {runner_mode}")
        self.task_id = task_id
        self.keep_workspace = bool(keep_workspace)
        self.no_step_budget = bool(no_step_budget)
        self.max_reasoning_steps = (
            max(1, int(max_reasoning_steps))
            if max_reasoning_steps is not None
            else None
        )
        self.progress = progress
        self.plugin_manager = plugin_manager

    def run(self) -> dict[str, Any]:
        cleanup_expired_sandboxes()
        eval_id = "eval_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        eval_dir = self.eval_root / eval_id
        owns_workspace_root = self.workspace_root is None
        workspaces_root = Path(
            tempfile.mkdtemp(prefix="coding-benchmark-")
            if owns_workspace_root
            else self.workspace_root
        )
        workspaces_root.mkdir(parents=True, exist_ok=True)
        eval_dir.mkdir(parents=True, exist_ok=True)

        tasks = load_benchmark(self.benchmark_path)
        if self.task_id:
            tasks = [task for task in tasks if task.id == self.task_id]
            if not tasks:
                raise ValueError(f"Unknown benchmark task id: {self.task_id}")
        self._emit("eval_started", {
            "eval_id": eval_id,
            "task_count": len(tasks),
            "runner_mode": self.runner_mode,
            "workspace_root": str(workspaces_root),
        })
        rows = []
        for index, task in enumerate(tasks, start=1):
            self._emit("task_started", {
                "index": index,
                "total": len(tasks),
                "id": task.id,
                "category": task.category,
            })
            started = time.perf_counter()
            row = self.run_task(task, eval_dir=eval_dir, workspaces_root=workspaces_root)
            row["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
            rows.append(row)
            self._emit("task_finished", {
                "index": index,
                "total": len(tasks),
                "id": task.id,
                "category": task.category,
                "status": row["status"],
                "failure_reason": row.get("failure_reason", ""),
                "duration_ms": row["duration_ms"],
                "reasoning_steps": row.get("reasoning_steps", 0),
                "tool_calls": row.get("tool_calls", 0),
            })
        payload = {
            "schema_version": 1,
            "eval_id": eval_id,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "runner": {
                "mode": self.runner_mode,
                **self._runner_metadata(),
            },
            "benchmark": {
                "source": str(self.benchmark_path),
                "task_count": len(tasks),
                "task_id": self.task_id or "",
                "no_step_budget": self.no_step_budget,
                "max_reasoning_steps": self.max_reasoning_steps,
            },
            "workspace_root": str(workspaces_root),
            "workspace_retained": self.keep_workspace or not owns_workspace_root,
            "summary": summarize_rows(rows),
            "rows": rows,
        }
        (eval_dir / "summary.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        (eval_dir / "rows.json").write_text(
            json.dumps(rows, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        self._plugin_manager().after_eval(eval_dir=eval_dir, payload=payload)
        self._emit("eval_finished", {
            "eval_id": eval_id,
            "summary": payload["summary"],
            "eval_dir": str(eval_dir),
        })
        if owns_workspace_root and not self.keep_workspace:
            shutil.rmtree(workspaces_root, ignore_errors=True)
        return payload

    def _plugin_manager(self) -> PluginManager:
        if self.plugin_manager is None:
            self.plugin_manager = PluginManager(
                [EvalReportPlugin()],
                workspace=self.repo_root,
                tool_registry=build_lead_tool_registry(),
            )
        return self.plugin_manager

    def run_task(self, task: BenchmarkTask, *, eval_dir: Path, workspaces_root: Path) -> dict[str, Any]:
        workspace = self._fresh_workspace(task, workspaces_root)
        _init_git_repo(workspace)
        trace_store = TraceStore(eval_dir / "runs")
        sessions = SessionManager()
        provider, model = self._provider_for_task(task)
        effective_step_budget = self._effective_step_budget(task)
        pipeline = Pipeline(
            tools=_tool_registry_for(task.allowed_tools),
            provider=provider,
            model=model,
            tool_executor=ToolExecutor([
                ShellSafetyHook(),
                ShellWorkspaceScopeHook(workspace),
                FileWriteScopeHook(workspace),
                ToolLoopGuardHook(),
                ToolTraceHook(),
            ]),
            context_builder=ContextBuilder(memory_store=MemoryStore(eval_dir / "memory" / task.id / "task")),
            max_reasoning_steps=effective_step_budget,
        )
        runner = TaskSessionRunner(
            sessions=sessions,
            base_pipeline=pipeline,
            global_memory=MemoryStore(eval_dir / "memory" / task.id / "global"),
            workspace_resolver=WorkspaceResolver(
                allowed_roots=[workspaces_root],
                default_workspace=workspace,
            ),
        )

        parent = Session(
            id=f"web:benchmark:{eval_dir.name}:{task.id}",
            current_mode="coding",
            metadata={
                "user_id": "benchmark",
                "user_role": "admin",
                "eval_id": eval_dir.name,
                "benchmark_task_id": task.id,
            },
        )
        run_state = RunState.create(
            session_id=parent.id,
            channel="benchmark",
            chat_id=task.id,
            user_id="benchmark",
            user_role="admin",
            mode="coding",
            execution_path="task_session",
            metadata={"benchmark_task_id": task.id, "eval_id": eval_dir.name},
        )
        trace_store.start_run(run_state)

        error = ""
        reply = ""
        try:
            reply = runner.run_coding_task(
                parent_session=parent,
                user_text=task.prompt,
                profile=CODING_PROFILE,
                workspace_root=str(workspace),
                run_state=run_state,
                trace_store=trace_store,
            )
            run_state.finish_success(reply)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            run_state.fail(exc)
        finally:
            trace_store.write_run_state(run_state)
            trace_store.write_report(run_state, {
                "benchmark_task_id": task.id,
                "category": task.category,
                "workspace_root": str(workspace),
            })
            sessions.close()

        run_dir = trace_store.run_dir(run_state)
        verifier = verify_task(workspace=workspace, run_dir=run_dir, task=task)
        within_budget = (
            True
            if self.no_step_budget
            else run_state.reasoning_steps <= effective_step_budget
        )
        verifier_passed = bool(verifier["passed"])
        workspace_diff_passed = _checks_passed(verifier, {"modified", "created", "not_modified"})
        trace_passed = _checks_passed(verifier, {"trace_event_exists", "tool_called", "tool_denied"})
        row = {
            "id": task.id,
            "category": task.category,
            "runner_mode": self.runner_mode,
            "status": "pass",
            "passed": True,
            "failure_category": "",
            "failure_reason": "",
            "prompt": task.prompt,
            "workspace_relpath": _relative_or_str(workspace, workspaces_root),
            "workspace_path": str(workspace),
            "run_id": run_state.run_id,
            "run_dir": str(run_dir),
            "allowed_tools": task.allowed_tools,
            "step_budget": task.step_budget,
            "effective_step_budget": None if self.no_step_budget else effective_step_budget,
            "budget_disabled": self.no_step_budget,
            "reasoning_steps": run_state.reasoning_steps,
            "tool_calls": run_state.tool_calls,
            "within_budget": within_budget,
            "verifier_passed": verifier_passed,
            "workspace_diff_passed": workspace_diff_passed,
            "trace_passed": trace_passed,
            "run_status": run_state.status,
            "final_answer": run_state.final_answer or reply,
            "error": error,
            "verifier": verifier,
        }
        row["passed"] = (
            row["run_status"] == "completed"
            and within_budget
            and verifier_passed
            and workspace_diff_passed
            and trace_passed
        )
        row["status"] = "pass" if row["passed"] else "fail"
        row["failure_category"] = "" if row["passed"] else failure_category(row)
        row["failure_reason"] = "" if row["passed"] else diagnose_failure(row)
        trace_store.write_report(run_state, {
            "benchmark_task_id": task.id,
            "category": task.category,
            "workspace_root": str(workspace),
            "status": row["status"],
            "passed": row["passed"],
            "failure_category": row["failure_category"],
            "failure_reason": row["failure_reason"],
            "verifier": verifier,
            "within_budget": within_budget,
            "workspace_diff_passed": workspace_diff_passed,
            "trace_passed": trace_passed,
        })
        return row

    def _fresh_workspace(self, task: BenchmarkTask, workspaces_root: Path) -> Path:
        source = (self.repo_root / task.fixture_repo).resolve()
        if not source.is_dir():
            raise ValueError(f"Fixture repo does not exist: {task.fixture_repo}")
        destination = workspaces_root / task.id / source.name
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
        return destination

    def _provider_for_task(self, task: BenchmarkTask):
        if self.runner_mode == "scripted":
            return ScriptedProvider(task.script), "scripted-coding-benchmark"
        from runtime.bootstrap import get_model_pool

        model_pool = get_model_pool()
        return model_pool.routed_provider("coding"), model_pool.model_for("coding")

    def _runner_metadata(self) -> dict[str, Any]:
        if self.runner_mode == "scripted":
            return {
                "provider": "ScriptedProvider",
                "model": "scripted-coding-benchmark",
            }
        from runtime.bootstrap import get_model_pool

        model_pool = get_model_pool()

        return {
            "provider": "MODEL_POOL:coding",
            "model": model_pool.model_for("coding"),
        }

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.progress is not None:
            self.progress({"event": event, **payload})

    def _effective_step_budget(self, task: BenchmarkTask) -> int:
        if self.max_reasoning_steps is not None:
            return self.max_reasoning_steps
        if self.no_step_budget:
            return DEFAULT_UNBOUNDED_EVAL_MAX_REASONING_STEPS
        return task.step_budget


def run_coding_benchmark(
    *,
    benchmark_path: str | Path = DEFAULT_BENCHMARK_PATH,
    eval_root: str | Path = DEFAULT_EVAL_ROOT,
    workspace_root: str | Path | None = None,
    runner_mode: str = "scripted",
    task_id: str | None = None,
    keep_workspace: bool = False,
    no_step_budget: bool = False,
    max_reasoning_steps: int | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    plugin_manager: PluginManager | None = None,
) -> dict[str, Any]:
    return CodingBenchmarkHarness(
        benchmark_path=benchmark_path,
        eval_root=eval_root,
        workspace_root=workspace_root,
        runner_mode=runner_mode,
        task_id=task_id,
        keep_workspace=keep_workspace,
        no_step_budget=no_step_budget,
        max_reasoning_steps=max_reasoning_steps,
        progress=progress,
        plugin_manager=plugin_manager,
    ).run()


def diagnose_failure(row: dict[str, Any]) -> str:
    final_answer = str(row.get("final_answer") or "")
    if "工具推理步骤超过上限" in final_answer or "reasoning step" in final_answer.lower():
        return "budget_stop"
    if "没有可用的写文件工具" in final_answer or "no available write" in final_answer.lower():
        return "write_tool_not_visible"
    if row.get("run_status") != "completed":
        if row.get("error"):
            return "run_error"
        if not row.get("final_answer"):
            return "no_final_answer"
        return "run_not_completed"
    if not row.get("within_budget"):
        return "reasoning_budget_exceeded"

    checks = row.get("verifier", {}).get("checks", [])
    failed = [check for check in checks if not check.get("passed")]
    failed_names = {check.get("name") for check in failed}
    if "must_pass_command" in failed_names:
        return "test_command_failed"
    if "modified" in failed_names:
        return "expected_file_not_modified"
    if "not_modified" in failed_names:
        return "unexpected_file_modified"
    if "created" in failed_names:
        return "expected_file_not_created"
    if "file_contains" in failed_names:
        return "expected_content_missing"
    if "tool_called" in failed_names:
        return "expected_tool_not_called"
    if "tool_denied" in failed_names:
        return "expected_tool_denial_missing"
    if "trace_event_exists" in failed_names:
        return "trace_event_missing"
    if _has_many_missing_file_errors(row):
        return "missing_discovery_tool"
    if not row.get("workspace_diff_passed"):
        return "workspace_diff_mismatch"
    if not row.get("trace_passed"):
        return "trace_incomplete"
    if not row.get("verifier_passed"):
        return "verifier_failed"
    return "unknown"


def _has_many_missing_file_errors(row: dict[str, Any]) -> bool:
    checks_text = json.dumps(row.get("verifier", {}), ensure_ascii=False, default=str)
    final_answer = str(row.get("final_answer") or "")
    return (
        checks_text.count("No such file or directory") >= 3
        or final_answer.count("No such file or directory") >= 3
    )


def _tool_registry_for(allowed_tools: list[str]):
    allowed = set(allowed_tools) | {"tool_search", "recall_memory", "memorize", "load_skill"}
    registry = build_lead_tool_registry()
    for name in list(registry._tools):
        if name not in allowed:
            registry.unregister(name)
    for name in allowed_tools:
        tool = registry._tools.get(name)
        if tool is not None:
            tool.always_on = True
    return registry


def _tool_response(index: int, name: str, arguments: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id=f"call-{index}", name=name, arguments=arguments)],
        raw_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"call-{index}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }],
        },
    )


def _final_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        raw_message={"role": "assistant", "content": content},
    )


def _init_git_repo(workspace: Path) -> None:
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Benchmark"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "benchmark@example.invalid"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )


def _checks_passed(verifier: dict[str, Any], names: set[str]) -> bool:
    checks = [item for item in verifier.get("checks", []) if item.get("name") in names]
    return all(item.get("passed") for item in checks)


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)
