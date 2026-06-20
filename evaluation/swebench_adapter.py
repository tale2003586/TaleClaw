from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from agents.coding.runner import TaskSessionRunner
from memory.store import MemoryStore
from modes.coding import CODING_PROFILE
from runtime.context import ContextBuilder
from runtime.pipeline import Pipeline
from runtime.trace.run_state import RunState
from runtime.trace.trace_store import TraceStore
from runtime.workspace import WorkspaceResolver
from sessions.session import Session, SessionManager
from tools.executor import ToolExecutor
from tools.hooks import (
    FileWriteScopeHook,
    ShellSafetyHook,
    ShellWorkspaceScopeHook,
    ToolLoopGuardHook,
    ToolTraceHook,
)
from tools.tool_registry import build_lead_tool_registry


DEFAULT_SWEBENCH_DATASET = "princeton-nlp/SWE-bench_Lite"
DEFAULT_SWEBENCH_SPLIT = "test"
DEFAULT_SWEBENCH_EVAL_ROOT = Path(".evals/swebench")
DEFAULT_SWEBENCH_WORKSPACE_ROOT = Path(".evals/swebench_workspaces")


@dataclass(frozen=True)
class SweBenchInstance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    hints_text: str = ""

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "SweBenchInstance":
        missing = [
            key
            for key in ("instance_id", "repo", "base_commit", "problem_statement")
            if not str(record.get(key, "")).strip()
        ]
        if missing:
            raise ValueError(f"SWE-bench record is missing required fields: {', '.join(missing)}")
        return cls(
            instance_id=str(record["instance_id"]),
            repo=str(record["repo"]),
            base_commit=str(record["base_commit"]),
            problem_statement=str(record["problem_statement"]),
            hints_text=str(record.get("hints_text") or ""),
        )


@dataclass(frozen=True)
class SweBenchRunResult:
    instance: SweBenchInstance
    eval_dir: Path
    run_dir: Path
    workspace: Path
    predictions_path: Path
    model_patch: str
    run_state: RunState
    reply: str
    official_eval_command: list[str] | None = None


def load_swebench_instance(
    *,
    dataset_name: str = DEFAULT_SWEBENCH_DATASET,
    split: str = DEFAULT_SWEBENCH_SPLIT,
    instance_id: str,
) -> SweBenchInstance:
    try:
        from datasets import load_dataset
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency 'datasets'. Install it before running a real "
            "SWE-bench task, for example: pip install datasets"
        ) from exc

    dataset = load_dataset(dataset_name, split=split)
    for record in dataset:
        if str(record.get("instance_id")) == instance_id:
            return SweBenchInstance.from_record(dict(record))
    raise ValueError(
        f"Instance {instance_id!r} was not found in {dataset_name!r} split {split!r}."
    )


def build_swebench_prompt(instance: SweBenchInstance) -> str:
    hints = ""
    if instance.hints_text.strip():
        hints = f"\n\nHints from SWE-bench:\n{instance.hints_text.strip()}"
    return (
        "This is a SWE-bench bugfix task.\n"
        f"Instance ID: {instance.instance_id}\n"
        f"Repository: {instance.repo}\n"
        f"Base commit: {instance.base_commit}\n\n"
        "You are already working at the repository root for this instance. "
        "Use relative paths for files and shell commands. Do not change to an "
        "absolute path outside this workspace.\n\n"
        "Fix the issue described below by editing the repository implementation. "
        "Prefer the smallest correct change. Do not modify tests unless the problem "
        "statement explicitly requires test changes. When finished, leave the working "
        "tree containing only the intended fix so the final git diff is the answer.\n\n"
        f"Problem statement:\n{instance.problem_statement.strip()}"
        f"{hints}"
    )


def prediction_record(
    *,
    instance_id: str,
    model_name_or_path: str,
    model_patch: str,
) -> dict[str, str]:
    return {
        "instance_id": instance_id,
        "model_name_or_path": model_name_or_path,
        "model_patch": model_patch,
    }


def run_swebench_instance(
    *,
    instance: SweBenchInstance,
    eval_root: str | Path = DEFAULT_SWEBENCH_EVAL_ROOT,
    workspace_root: str | Path = DEFAULT_SWEBENCH_WORKSPACE_ROOT,
    model_name: str | None = None,
    max_reasoning_steps: int = 80,
    reuse_workspace: bool = False,
    progress: Callable[[dict[str, Any]], None] | None = None,
    swebench_repo: str | Path | None = None,
    evaluate: bool = False,
    dataset_name: str = DEFAULT_SWEBENCH_DATASET,
) -> SweBenchRunResult:
    eval_id = "swebench_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
    eval_dir = Path(eval_root) / eval_id
    workspace_parent = Path(workspace_root) / eval_id
    eval_dir.mkdir(parents=True, exist_ok=True)
    workspace_parent.mkdir(parents=True, exist_ok=True)

    _emit(progress, "workspace_prepare_started", {"instance_id": instance.instance_id})
    workspace = prepare_swebench_workspace(
        instance,
        workspace_parent=workspace_parent,
        reuse_workspace=reuse_workspace,
    )
    _emit(progress, "workspace_prepared", {"workspace": str(workspace)})

    from runtime.bootstrap import get_model_pool

    model_pool = get_model_pool()
    provider = model_pool.routed_provider("coding")
    model = model_name or model_pool.model_for("coding")
    trace_store = TraceStore(eval_dir / "runs")
    sessions = SessionManager()
    pipeline = Pipeline(
        tools=build_lead_tool_registry(),
        provider=provider,
        model=model,
        tool_executor=ToolExecutor([
            ShellSafetyHook(),
            ShellWorkspaceScopeHook(workspace),
            FileWriteScopeHook(workspace),
            ToolLoopGuardHook(),
            ToolTraceHook(),
        ]),
        context_builder=ContextBuilder(
            memory_store=MemoryStore(eval_dir / "memory" / instance.instance_id / "task")
        ),
        max_reasoning_steps=max(1, int(max_reasoning_steps)),
    )
    runner = TaskSessionRunner(
        sessions=sessions,
        base_pipeline=pipeline,
        global_memory=MemoryStore(eval_dir / "memory" / instance.instance_id / "global"),
        workspace_resolver=WorkspaceResolver(
            allowed_roots=[workspace_parent],
            default_workspace=workspace,
        ),
    )

    parent = Session(
        id=f"web:swebench:{eval_id}:{instance.instance_id}",
        current_mode="coding",
        metadata={
            "user_id": "swebench",
            "user_role": "admin",
            "eval_id": eval_id,
            "swebench_instance_id": instance.instance_id,
        },
    )
    run_state = RunState.create(
        session_id=parent.id,
        channel="swebench",
        chat_id=instance.instance_id,
        user_id="swebench",
        user_role="admin",
        mode="coding",
        execution_path="task_session",
        metadata={
            "swebench_instance_id": instance.instance_id,
            "eval_id": eval_id,
            "swebench_repo": instance.repo,
            "base_commit": instance.base_commit,
        },
    )
    trace_store.start_run(run_state)

    reply = ""
    try:
        _emit(progress, "agent_started", {"run_id": run_state.run_id})
        reply = runner.run_coding_task(
            parent_session=parent,
            user_text=build_swebench_prompt(instance),
            profile=CODING_PROFILE,
            workspace_root=str(workspace),
            run_state=run_state,
            trace_store=trace_store,
        )
        run_state.finish_success(reply)
    except Exception as exc:
        run_state.fail(exc)
        raise
    finally:
        trace_store.write_run_state(run_state)
        trace_store.write_report(run_state, {
            "swebench_instance_id": instance.instance_id,
            "workspace_root": str(workspace),
        })
        sessions.close()

    model_patch = git_diff(workspace)
    predictions_path = eval_dir / "predictions.jsonl"
    write_prediction(
        predictions_path,
        instance_id=instance.instance_id,
        model_name_or_path=model,
        model_patch=model_patch,
    )
    official_command = None
    if swebench_repo is not None:
        official_command = official_evaluation_command(
            swebench_repo=swebench_repo,
            dataset_name=dataset_name,
            predictions_path=predictions_path,
            run_id=eval_id,
            instance_id=instance.instance_id,
        )
    (eval_dir / "result.json").write_text(
        json.dumps({
            "instance_id": instance.instance_id,
            "repo": instance.repo,
            "base_commit": instance.base_commit,
            "workspace": str(workspace),
            "run_id": run_state.run_id,
            "run_dir": str(trace_store.run_dir(run_state)),
            "predictions_path": str(predictions_path),
            "patch_bytes": len(model_patch.encode("utf-8")),
            "official_eval_command": official_command or [],
            "run_status": run_state.status,
        }, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    _emit(progress, "prediction_written", {"predictions_path": str(predictions_path)})

    if evaluate and official_command is not None:
        _emit(progress, "official_eval_started", {"command": official_command})
        subprocess.run(official_command, cwd=Path(swebench_repo), check=True)
        _emit(progress, "official_eval_finished", {"run_id": eval_id})

    return SweBenchRunResult(
        instance=instance,
        eval_dir=eval_dir,
        run_dir=trace_store.run_dir(run_state),
        workspace=workspace,
        predictions_path=predictions_path,
        model_patch=model_patch,
        run_state=run_state,
        reply=reply,
        official_eval_command=official_command,
    )


def prepare_swebench_workspace(
    instance: SweBenchInstance,
    *,
    workspace_parent: str | Path,
    reuse_workspace: bool = False,
) -> Path:
    workspace_parent = Path(workspace_parent)
    workspace = workspace_parent / safe_instance_dir(instance.instance_id)
    if workspace.exists() and not reuse_workspace:
        shutil.rmtree(workspace)
    if workspace.exists():
        _run_git(["fetch", "--all", "--tags"], cwd=workspace)
        _run_git(["checkout", instance.base_commit], cwd=workspace)
        _run_git(["reset", "--hard", instance.base_commit], cwd=workspace)
        _run_git(["clean", "-fdx"], cwd=workspace)
    else:
        workspace.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", repo_clone_url(instance.repo), str(workspace)],
            check=True,
            capture_output=True,
            text=True,
        )
        _run_git(["checkout", instance.base_commit], cwd=workspace)
    _run_git(["config", "user.name", "SWE-bench"], cwd=workspace)
    _run_git(["config", "user.email", "swebench@example.invalid"], cwd=workspace)
    return workspace


def repo_clone_url(repo: str) -> str:
    repo = repo.strip()
    if repo.startswith(("http://", "https://", "git@")):
        return repo
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError(f"Invalid GitHub repo name: {repo!r}")
    owner, name = repo.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."} or owner.startswith(".") or name.startswith("."):
        raise ValueError(f"Invalid GitHub repo name: {repo!r}")
    return f"https://github.com/{repo}.git"


def safe_instance_dir(instance_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", instance_id.strip())
    if not value:
        raise ValueError("Empty SWE-bench instance id.")
    return value


def git_diff(workspace: str | Path) -> str:
    result = subprocess.run(
        ["git", "diff", "--binary"],
        cwd=Path(workspace),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def write_prediction(
    path: str | Path,
    *,
    instance_id: str,
    model_name_or_path: str,
    model_patch: str,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = prediction_record(
        instance_id=instance_id,
        model_name_or_path=model_name_or_path,
        model_patch=model_patch,
    )
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def official_evaluation_command(
    *,
    swebench_repo: str | Path,
    dataset_name: str,
    predictions_path: str | Path,
    run_id: str,
    instance_id: str | None = None,
    max_workers: int = 1,
) -> list[str]:
    command = [
        "python",
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        dataset_name,
        "--predictions_path",
        str(Path(predictions_path).resolve()),
        "--max_workers",
        str(max_workers),
        "--run_id",
        run_id,
    ]
    if instance_id:
        command.extend(["--instance_ids", instance_id])
    return command


def _run_git(args: list[str], *, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _emit(progress: Callable[[dict[str, Any]], None] | None, event: str, payload: dict[str, Any]) -> None:
    if progress is not None:
        progress({"event": event, **payload})
