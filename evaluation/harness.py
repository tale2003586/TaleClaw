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

from applications.coding.runner import CodingApplication
from evaluation.metrics import failure_category, summarize_rows
from evaluation.provenance import evaluation_manifest
from evaluation.outcomes import finish_generation, verification_result
from evaluation.session_manager import EvaluationSessionManager
from evaluation.task_schema import BenchmarkTask, load_benchmark
from evaluation.verifiers import verify_task
from models.provider import LLMResponse, ToolCall
from agents.definitions import CODING_AGENT_SPEC
from plugins import PluginManager
from plugins.eval_report import EvalReportPlugin
from runtime.context import ContextBuilder
from applications.coding.context_state import build_coding_context_view
from runtime.runtime import Runtime
from runtime.trace.run_state import RunState
from runtime.trace.trace_store import TraceStore
from runtime.workspace import WorkspaceResolver
from runtime.sessions.session import Session, SessionManager
from tools.executor import ToolExecutor
from tools.handlers import cleanup_expired_sandboxes
from tools.hooks import (
    ArtifactAccessGuardHook,
    FileWriteScopeHook,
    ShellSafetyHook,
    ShellWorkspaceScopeHook,
    TaskStateLifecycleGuardHook,
    ToolLoopGuardHook,
    ToolResultStoreHook,
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
        context_policy: str | dict = "current",
        execution_budget_limits: dict | None = None,
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
        from runtime.context.policies import context_policy as parse_policy
        from types import SimpleNamespace
        from dataclasses import asdict
        self.context_policy = asdict(parse_policy(SimpleNamespace(metadata={"context_policy": context_policy})))
        self.execution_budget_limits = dict(execution_budget_limits or {})
        permitted = {"max_model_attempts", "max_tool_attempts", "max_known_tokens", "max_seconds"}
        if set(self.execution_budget_limits) - permitted or any(type(v) is not int or v < 1 for v in self.execution_budget_limits.values()):
            raise ValueError("Execution budget limits must be positive integers with known keys")

    def run(self) -> dict[str, Any]:
        cleanup_expired_sandboxes()
        tasks = load_benchmark(self.benchmark_path)
        if self.task_id:
            tasks = [task for task in tasks if task.id == self.task_id]
            if not tasks:
                raise ValueError(f"Unknown benchmark task id: {self.task_id}")
        eval_id = "eval_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        eval_dir = self.eval_root / eval_id
        owns_workspace_root = self.workspace_root is None
        workspaces_root = Path(
            tempfile.mkdtemp(prefix="coding-benchmark-")
            if owns_workspace_root else self.workspace_root
        )
        workspaces_root.mkdir(parents=True, exist_ok=True)
        eval_dir.mkdir(parents=True, exist_ok=True)
        runner_metadata = self._runner_metadata()
        manifest = evaluation_manifest(
            dataset_path=self.benchmark_path, configuration={
                "runner_mode": self.runner_mode, "model_purpose": "coding",
                "model_name": runner_metadata.get("model"),
                "max_reasoning_steps": self.max_reasoning_steps,
                "no_step_budget": self.no_step_budget, "selected_ids": [task.id for task in tasks],
                "context_policy": self.context_policy, "execution_budget_limits": self.execution_budget_limits,
            },
        )
        rows = []
        payload = {
            "schema_version": 2, "status": "running", "manifest": manifest, "manifest_after": None,
            "eval_id": eval_id, "captured_at": datetime.now(timezone.utc).isoformat(),
            "runner": {"mode": self.runner_mode, **runner_metadata},
            "benchmark": {"source": str(self.benchmark_path), "task_count": len(tasks),
                "task_id": self.task_id or "", "no_step_budget": self.no_step_budget,
                "max_reasoning_steps": self.max_reasoning_steps},
            "expected_task_ids": [task.id for task in tasks],
            "workspace_root": str(workspaces_root),
            "workspace_retained": self.keep_workspace or not owns_workspace_root,
            "rows": rows,
        }
        # Matrix callers can recover this ledger even when run() must re-raise
        # KeyboardInterrupt; the same snapshot is durably written after each row.
        self.latest_result = payload
        self.ledger_path = eval_dir / "summary.json"
        active_task_id = None
        try:
            self._write_ledger(eval_dir, payload)
            self._emit("eval_started", {"eval_id": eval_id, "task_count": len(tasks),
                "runner_mode": self.runner_mode, "workspace_root": str(workspaces_root)})
            for index, task in enumerate(tasks, start=1):
                active_task_id = task.id
                self._emit("task_started", {"index": index, "total": len(tasks),
                    "id": task.id, "category": task.category})
                started = time.perf_counter()
                try:
                    row = self.run_task(task, eval_dir=eval_dir, workspaces_root=workspaces_root)
                except Exception as exc:
                    row = {"id": task.id, "category": task.category, "runner_mode": self.runner_mode,
                        "status": "error", "passed": None, "execution_status": "unknown", "run_status": "unknown",
                        "verification": verification_result("inconclusive", reason="evaluation_error", error_type=type(exc).__name__),
                        "failure_category": "evaluation_error", "failure_reason": str(exc),
                        "error": f"{type(exc).__name__}: {exc}"}
                row["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
                rows.append(row)
                active_task_id = None
                # Persist before callbacks: stdout/progress must never be the
                # sole surviving copy of a completed task result.
                self._write_ledger(eval_dir, payload)
                self._emit("task_finished", {"index": index, "total": len(tasks), "id": task.id,
                    "category": task.category, "status": row["status"],
                    "failure_reason": row.get("failure_reason", ""), "duration_ms": row["duration_ms"],
                    "reasoning_steps": row.get("reasoning_steps", 0), "tool_calls": row.get("tool_calls", 0)})
            payload["status"] = "completed"
            payload["manifest_after"] = evaluation_manifest(dataset_path=self.benchmark_path, configuration=manifest["configuration"])
            self._write_ledger(eval_dir, payload)
            self._plugin_manager().after_eval(eval_dir=eval_dir, payload=payload)
            self._emit("eval_finished", {"eval_id": eval_id, "summary": payload["summary"], "eval_dir": str(eval_dir)})
            return payload
        except BaseException as exc:
            interrupted = not isinstance(exc, Exception)
            payload.update(status="interrupted" if interrupted else "error", error_type=type(exc).__name__, error=str(exc))
            recorded = {row["id"] for row in rows}
            for task in tasks:
                if task.id in recorded:
                    continue
                in_flight = task.id == active_task_id
                rows.append({"id": task.id, "category": task.category, "runner_mode": self.runner_mode,
                    "status": "interrupted" if in_flight else "not_run", "passed": None,
                    "execution_status": "interrupted" if in_flight else "not_run",
                    "run_status": "interrupted" if in_flight else "not_run",
                    "verification": verification_result("inconclusive", reason=(
                        "evaluation_interrupted" if in_flight else "not_run_after_interruption") if interrupted else "harness_error",
                        error_type=type(exc).__name__), "failure_category": "evaluation_interrupted" if interrupted else "evaluation_error"})
            # Interrupted temporary workspaces retain their diagnostic state,
            # matching the former exception path and avoiding a cleanup race.
            payload["workspace_retained"] = True
            raise
        finally:
            if payload["manifest_after"] is None:
                try:
                    payload["manifest_after"] = evaluation_manifest(dataset_path=self.benchmark_path, configuration=manifest["configuration"])
                except Exception as fingerprint_error:
                    # Unknown provenance excludes comparisons; it must not
                    # erase completed rows or mask the original interruption.
                    payload["manifest_after_error"] = f"{type(fingerprint_error).__name__}: {fingerprint_error}"
            self._write_ledger(eval_dir, payload)
            if payload["status"] == "completed" and owns_workspace_root and not self.keep_workspace:
                shutil.rmtree(workspaces_root, ignore_errors=True)

    def _write_ledger(self, eval_dir: Path, payload: dict) -> None:
        payload["summary"] = summarize_rows(payload["rows"])
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        for name, value in (("rows.json", payload["rows"]), ("summary.json", payload)):
            target = eval_dir / name
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
            temporary.replace(target)

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
        scripted = self.runner_mode == "scripted"
        # This runner owns an isolated fixture workspace; the verifier needs
        # its detailed diff evidence. Production trace defaults remain private.
        # Benchmarks own fixture state for both model modes. A real-model trial
        # must not create/delete rows in the user's production session database.
        trace_store = TraceStore(eval_dir / "runs", index_enabled=False, capture_content=True)
        sessions = EvaluationSessionManager()
        provider, model = self._provider_for_task(task)
        effective_step_budget = self._effective_step_budget(task)
        runtime = Runtime(
            tools=_tool_registry_for(task.allowed_tools),
            provider=provider,
            model=model,
            tool_executor=ToolExecutor([
                ShellSafetyHook(),
                ShellWorkspaceScopeHook(workspace),
                FileWriteScopeHook(workspace),
                TaskStateLifecycleGuardHook(),
                ArtifactAccessGuardHook(),
                ToolLoopGuardHook(),
                ToolResultStoreHook(),
                ToolTraceHook(),
                *self._extra_tool_hooks(workspace),
            ]),
            context_builder=ContextBuilder(
                coding_context_view_builder=build_coding_context_view,
            ),
            max_reasoning_steps=effective_step_budget,
        )
        runner = CodingApplication(
            sessions=sessions,
            base_runtime=runtime,
            workspace_resolver=WorkspaceResolver(
                allowed_roots=[workspaces_root],
                default_workspace=workspace,
            ),
        )

        parent = Session(
            id=f"web:benchmark:{eval_dir.name}:{task.id}",
            active_agent="coding",
            metadata={
                "user_id": "benchmark",
                "user_role": "admin",
                "eval_id": eval_dir.name,
                "benchmark_task_id": task.id,
                "context_policy": self.context_policy,
            },
        )
        run_state = RunState.create(
            session_id=parent.id,
            channel="benchmark",
            chat_id=task.id,
            user_id="benchmark",
            user_role="admin",
            mode="coding",
            execution_path="coding_application",
            metadata={"benchmark_task_id": task.id, "eval_id": eval_dir.name},
        )
        trace_store.start_run(run_state)

        from runtime.execution.budget import ExecutionBudget, execution_scope
        limits = dict(self.execution_budget_limits)
        max_seconds = limits.pop("max_seconds", 900)
        budget = ExecutionBudget(deadline=time.monotonic() + max_seconds, **limits)
        error = ""
        error_type = ""
        policy_observation = {}
        reply = ""
        try:
            with execution_scope(budget=budget):
                reply = runner.run_coding_task(
                    parent_session=parent,
                    user_text=task.prompt,
                    agent_spec=CODING_AGENT_SPEC,
                    workspace_root=str(workspace),
                    run_state=run_state,
                    trace_store=trace_store,
                )
            finish_generation(run_state, reply)
        except Exception as exc:
            error_type = type(exc).__name__
            error = f"{error_type}: {exc}"
            run_state.fail(exc)
        finally:
            from applications.coding.task_lifecycle import ACTIVE_CODING_TASK_ID
            task_session = sessions.get_existing("task:" + parent.metadata.get(ACTIVE_CODING_TASK_ID, ""))
            if task_session is not None:
                policy_observation = {"resolution": task_session.metadata.get("context_policy_resolution"),
                                      "last_plan": task_session.metadata.get("context_policy_audit"),
                                      "compaction_request": task_session.metadata.get("context_compaction_request")}
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
        verifier_passed = verifier["passed"]
        verification = verification_result(
            verifier["outcome"], reason=verifier["reason"], verifier_id="fixture.v1",
            run_id=run_state.run_id, error_type=verifier.get("error_type"),
        )
        valid = verifier["outcome"] in {"pass", "fail"}
        workspace_diff_passed = _checks_passed(verifier, {"modified", "created", "not_modified"}) if valid else None
        trace_passed = _checks_passed(verifier, {"trace_event_exists", "tool_called", "tool_denied"}) if valid else None
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
            "execution_status": run_state.status,
            "verification": verification,
            "final_answer": run_state.final_answer or reply,
            "error": error,
            "verifier": verifier,
            "context_policy": self.context_policy,
            "execution_budget": budget.snapshot(),
            "error_type": error_type,
            "policy_observation": policy_observation,
            "actual_model_calls": _actual_model_calls(run_dir),
            "context_intervention": _context_intervention_evidence(run_dir),
            "tool_access_proxies": _tool_access_proxies(run_dir),
        }
        # A safety task may correctly stop while its behavioral checks pass.
        # Execution health and budget compliance remain separate observations;
        # neither a normal return nor a stop can replace the verifier result.
        row["passed"] = bool(verifier_passed) if valid else None
        row["status"] = ("pass" if row["passed"] else "fail") if valid else verifier["outcome"]
        row["failure_category"] = "" if row["passed"] else failure_category(row)
        row["failure_reason"] = "" if row["passed"] else diagnose_failure(row)
        trace_store.write_report(run_state, {
            "benchmark_task_id": task.id,
            "category": task.category,
            "workspace_root": str(workspace),
            "status": row["status"],
            "passed": row["passed"],
            "execution_status": run_state.status,
            "verification": verification,
            "failure_category": row["failure_category"],
            "failure_reason": row["failure_reason"],
            "verifier": verifier,
            "within_budget": within_budget,
            "workspace_diff_passed": workspace_diff_passed,
            "trace_passed": trace_passed,
        })
        return row

    def _extra_tool_hooks(self, workspace: Path) -> list:
        """Optional isolated benchmark observers; ordinary runners add none."""
        return []

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
        from applications.bootstrap import get_model_pool

        model_pool = get_model_pool()
        return model_pool.routed_provider("coding"), model_pool.model_for("coding")

    def _runner_metadata(self) -> dict[str, Any]:
        if self.runner_mode == "scripted":
            return {
                "provider": "ScriptedProvider",
                "model": "scripted-coding-benchmark",
            }
        from applications.bootstrap import get_model_pool

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
    if (row.get("verification") or {}).get("outcome") in {"not_run", "inconclusive"}:
        return str(row["verification"].get("reason") or "verification_unknown")
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
            from tools.spec import ToolExposure

            tool.exposure = ToolExposure.PRELOADED
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


def _actual_model_calls(run_dir: Path) -> list[dict]:
    path = run_dir / "trace.jsonl"
    result = []
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "model.call.completed":
            continue
        payload = event.get("payload") or {}
        metadata = payload.get("provider_metadata") or {}
        native = metadata.get("native_context") or {}
        result.append({"model": metadata.get("selected_model") or native.get("model") or payload.get("model"),
                       "profile": metadata.get("selected_profile"),
                       "policy": payload.get("context_policy") or (payload.get("context_policy_resolution") or {}).get("effective"),
                       "provider": payload.get("provider"), "usage": payload.get("usage")})
    return result


def _tool_access_proxies(run_dir: Path) -> dict:
    """Trace proxies only: same-argument reads are not proof of wasted investigation."""
    path = run_dir / "trace.jsonl"
    unknown = {"trace_available": False, "repeat_read_file_calls": None,
               "recovery_attempts": None, "recovery_tool_successes": None,
               "read_signature_coverage_complete": False,
               "interpretation": "Tool-status and exact-input proxies, not semantic investigation or evidence recovery quality."}
    if not path.is_file():
        return unknown
    seen_calls, signatures = set(), set()
    repeated = recoveries = successes = 0
    complete = recovery_complete = True
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            complete = recovery_complete = False
            continue
        if event.get("event") not in {"tool.call.completed", "tool.call.failed"}:
            continue
        payload = event.get("payload") or {}
        call_id = payload.get("tool_call_id")
        name = payload.get("tool_name")
        if name not in {"read_file", "read_artifact", "retrieve_tool_result"}:
            continue
        if not call_id:
            complete = False
            if name != "read_file":
                recovery_complete = False
            continue
        identity = (event.get("run_id"), event.get("step"), call_id)
        if identity in seen_calls:
            continue
        seen_calls.add(identity)
        if name != "read_file":
            recoveries += 1
            successes += payload.get("status") == "success"
            if payload.get("status") not in {"success", "error", "denied", "timeout", "cancelled"}:
                recovery_complete = False
            continue
        arguments = payload.get("final_arguments")
        if not isinstance(arguments, dict):
            try:
                arguments = json.loads(payload.get("final_arguments_preview") or "")
            except (ValueError, TypeError):
                arguments = None
        signature = ("hash", payload["final_arguments_sha256"]) if payload.get("final_arguments_sha256") else None
        if isinstance(arguments, dict):
            signature = ("json", json.dumps(arguments, sort_keys=True, ensure_ascii=False))
        if signature is None:
            complete = False
            continue
        repeated += signature in signatures
        signatures.add(signature)
    return {**unknown, "trace_available": True, "repeat_read_file_calls": repeated if complete else None,
            "repeat_read_file_calls_known": repeated, "read_signature_coverage_complete": complete,
            "recovery_attempts": recoveries if recovery_complete else None,
            "recovery_tool_successes": successes if recovery_complete else None,
            "recovery_attempts_known": recoveries, "recovery_tool_successes_known": successes,
            "recovery_coverage_complete": recovery_complete}


def _context_intervention_evidence(run_dir: Path) -> dict:
    """Count submitted-request changes; policy selection alone is not evidence."""
    path = run_dir / "trace.jsonl"
    result = {"schema_version": 1, "status": "unknown", "scope": "runtime_model_requests_after_context_guard",
              "counting": "request exposures, not unique observations or billed token savings",
              "requested_calls": 0, "completed_calls": 0, "audited_calls": 0,
              "masked_observations": 0, "dropped_groups": 0, "snapshots_used": 0,
              "native_compactions": 0, "planner_token_reduction_estimate": 0,
              "hard_trim_calls": 0, "intervention_observed": False, "reasons": []}
    if not path.is_file():
        result["reasons"].append("missing_trace")
        return result
    started, completed, snapshots, trims = {}, {}, set(), set()
    malformed = False
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("not an event")
        except (TypeError, ValueError):
            malformed = True
            continue
        name = event.get("event")
        key = event.get("span_id") or ("step", event.get("step"))
        if name == "model.call.started":
            started[key] = event
        elif name == "model.call.completed":
            completed[key] = event
        elif name == "context_emergency_trim":
            trims.add(event.get("step"))
    result.update(requested_calls=len(started), completed_calls=len(completed), hard_trim_calls=len(trims))
    reasons = result["reasons"]
    if malformed:
        reasons.append("malformed_trace")
    if not started:
        reasons.append("missing_request_audit")
    if set(completed) - set(started):
        reasons.append("completed_call_without_request_audit")
    for key, event in started.items():
        payload = event.get("payload") or {}
        audit = (payload.get("context_summary") or {}).get("context_intervention")
        if not isinstance(audit, dict) or audit.get("schema_version") != 1 or audit.get("audit_complete") is not True:
            reasons.append("missing_or_incomplete_request_audit")
            continue
        declared_policy = (payload.get("context_summary") or {}).get("context_policy")
        final_policy = ((completed.get(key) or {}).get("payload") or {}).get("context_policy")
        if audit.get("policy") != declared_policy or final_policy is not None and final_policy != declared_policy:
            reasons.append("request_policy_audit_mismatch")
            continue
        if audit.get("policy") == "native":
            final = (completed.get(key) or {}).get("payload") or {}
            metadata = final.get("provider_metadata") or {}
            native = metadata.get("native_context") or {}
            count = native.get("compaction_item_count")
            if metadata.get("context_policy") != "native" or type(count) is not int or count < 0:
                reasons.append("missing_native_commit_audit")
                continue
            result["native_compactions"] += count + int("compaction_usage" in metadata)
        else:
            values = [audit.get(name) for name in ("masked_observations", "dropped_groups")]
            if any(type(value) is not int or value < 0 for value in values):
                reasons.append("invalid_request_audit_counts")
                continue
            result["masked_observations"] += values[0]
            result["dropped_groups"] += values[1]
            if audit.get("snapshot_id"):
                snapshots.add(str(audit["snapshot_id"]))
            estimate = audit.get("planner_token_reduction_estimate")
            if type(estimate) is int and estimate >= 0:
                result["planner_token_reduction_estimate"] += estimate
        result["audited_calls"] += 1
    result["snapshots_used"] = len(snapshots)
    result["intervention_observed"] = any(result[name] > 0 for name in
        ("masked_observations", "dropped_groups", "snapshots_used", "native_compactions"))
    result["reasons"] = sorted(set(reasons))
    if not reasons and result["audited_calls"] == len(started) and started:
        result["status"] = "observed" if result["intervention_observed"] else "none"
    return result
