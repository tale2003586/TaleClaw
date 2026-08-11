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

from applications.coding.runner import CodingApplication
from agents.definitions import CODING_AGENT_SPEC
from runtime.context import ContextBuilder
from applications.coding.context_state import build_coding_context_view
from runtime.runtime import Runtime
from runtime.trace.run_state import RunState
from runtime.trace.trace_store import TraceStore
from runtime.workspace import WorkspaceResolver
from runtime.sessions.session import Session, SessionManager
from tools.executor import ToolExecutor
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


DEFAULT_SWEBENCH_DATASET = "princeton-nlp/SWE-bench_Lite"
DEFAULT_SWEBENCH_VERIFIED_DATASET = "SWE-bench/SWE-bench_Verified"
FALLBACK_SWEBENCH_VERIFIED_DATASET = "princeton-nlp/SWE-bench_Verified"
DEFAULT_SWEBENCH_SPLIT = "test"
DEFAULT_SWEBENCH_EVAL_ROOT = Path(".evals/swebench")
DEFAULT_SWEBENCH_WORKSPACE_ROOT = Path(".evals/swebench_workspaces")
DEFAULT_SWEBENCH_REPO_CACHE_ROOT = Path(".evals/swebench_repo_cache")
DEFAULT_GIT_TIMEOUT_SECONDS = 600


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
    records_path: str | Path | None = None,
) -> SweBenchInstance:
    records = (
        load_swebench_records_from_file(records_path)
        if records_path
        else load_swebench_records(dataset_name=dataset_name, split=split)
    )
    for record in records:
        if str(record.get("instance_id")) == instance_id:
            return SweBenchInstance.from_record(dict(record))
    source = str(records_path) if records_path else f"{dataset_name!r} split {split!r}"
    raise ValueError(f"Instance {instance_id!r} was not found in {source}.")


def load_swebench_instances(
    *,
    dataset_name: str = DEFAULT_SWEBENCH_DATASET,
    split: str = DEFAULT_SWEBENCH_SPLIT,
    instance_ids: list[str] | None = None,
    limit: int = 10,
    offset: int = 0,
    records_path: str | Path | None = None,
) -> list[SweBenchInstance]:
    records = (
        load_swebench_records_from_file(records_path)
        if records_path
        else load_swebench_records(dataset_name=dataset_name, split=split)
    )
    wanted = [item.strip() for item in (instance_ids or []) if item.strip()]
    if wanted:
        by_id = {str(record.get("instance_id")): record for record in records}
        missing = [item for item in wanted if item not in by_id]
        if missing:
            raise ValueError(
                f"Unknown SWE-bench instance id(s): {', '.join(missing)}"
            )
        return [SweBenchInstance.from_record(dict(by_id[item])) for item in wanted]

    start = max(0, int(offset or 0))
    stop = start + max(1, int(limit or 10))
    return [SweBenchInstance.from_record(dict(record)) for record in records[start:stop]]


def load_swebench_records_from_file(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path).expanduser()
    if not source.exists():
        raise FileNotFoundError(f"SWE-bench records file not found: {source}")
    text = source.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"SWE-bench records file is empty: {source}")

    if source.suffix.lower() == ".jsonl":
        records = [
            json.loads(line)
            for line in text.splitlines()
            if line.strip()
        ]
    else:
        payload = json.loads(text)
        if isinstance(payload, list):
            records = payload
        elif isinstance(payload, dict):
            records = _records_from_mapping(payload)
        else:
            raise ValueError(
                f"SWE-bench records file must contain a JSON array/object or JSONL records: {source}"
            )

    if not all(isinstance(record, dict) for record in records):
        raise ValueError(f"SWE-bench records file contains non-object rows: {source}")
    return [dict(record) for record in records]


def _records_from_mapping(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("instances", "selected_instances", "rows", "records", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    if all(key in payload for key in ("instance_id", "repo", "base_commit", "problem_statement")):
        return [payload]
    raise ValueError(
        "SWE-bench records JSON object must contain one of: "
        "instances, selected_instances, rows, records, data."
    )


def load_swebench_records(
    *,
    dataset_name: str = DEFAULT_SWEBENCH_DATASET,
    split: str = DEFAULT_SWEBENCH_SPLIT,
) -> list[dict[str, Any]]:
    try:
        from datasets import load_dataset
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency 'datasets'. Install it before running a real "
            "SWE-bench task, for example: pip install datasets"
        ) from exc

    errors = []
    for candidate in dataset_name_candidates(dataset_name):
        try:
            dataset = load_dataset(candidate, split=split)
            return [dict(record) for record in dataset]
        except Exception as exc:
            errors.append(f"{candidate}: {type(exc).__name__}: {exc}")
    raise RuntimeError(
        f"Failed to load SWE-bench dataset {dataset_name!r} split {split!r}.\n"
        + "\n".join(errors)
    )


def dataset_name_candidates(dataset_name: str) -> list[str]:
    normalized = str(dataset_name or "").strip()
    lowered = normalized.lower().replace("_", "-")
    if lowered in {"verified", "swe-verified", "swebench-verified", "swe-bench-verified"}:
        return [DEFAULT_SWEBENCH_VERIFIED_DATASET, FALLBACK_SWEBENCH_VERIFIED_DATASET]
    if normalized == DEFAULT_SWEBENCH_VERIFIED_DATASET:
        return [DEFAULT_SWEBENCH_VERIFIED_DATASET, FALLBACK_SWEBENCH_VERIFIED_DATASET]
    if normalized == FALLBACK_SWEBENCH_VERIFIED_DATASET:
        return [FALLBACK_SWEBENCH_VERIFIED_DATASET, DEFAULT_SWEBENCH_VERIFIED_DATASET]
    return [normalized or DEFAULT_SWEBENCH_DATASET]


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
    repo_cache_root: str | Path | None = DEFAULT_SWEBENCH_REPO_CACHE_ROOT,
    clone_retries: int = 2,
    git_timeout_seconds: int = DEFAULT_GIT_TIMEOUT_SECONDS,
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
        repo_cache_root=repo_cache_root,
        clone_retries=clone_retries,
        git_timeout_seconds=git_timeout_seconds,
    )
    _emit(progress, "workspace_prepared", {"workspace": str(workspace)})

    from applications.bootstrap import get_model_pool

    model_pool = get_model_pool()
    provider = model_pool.routed_provider("coding")
    model = model_name or model_pool.model_for("coding")
    trace_store = TraceStore(eval_dir / "runs")
    sessions = SessionManager()
    pipeline = Runtime(
        tools=build_lead_tool_registry(),
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
        ]),
        context_builder=ContextBuilder(
            coding_context_view_builder=build_coding_context_view,
        ),
        max_reasoning_steps=max(1, int(max_reasoning_steps)),
    )
    runner = CodingApplication(
        sessions=sessions,
        base_pipeline=pipeline,
        workspace_resolver=WorkspaceResolver(
            allowed_roots=[workspace_parent],
            default_workspace=workspace,
        ),
    )

    parent = Session(
        id=f"web:swebench:{eval_id}:{instance.instance_id}",
        active_agent="coding",
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
        execution_path="coding_application",
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
            profile=CODING_AGENT_SPEC,
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
    repo_cache_root: str | Path | None = DEFAULT_SWEBENCH_REPO_CACHE_ROOT,
    clone_retries: int = 2,
    git_timeout_seconds: int = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> Path:
    workspace_parent = Path(workspace_parent)
    workspace = workspace_parent / safe_instance_dir(instance.instance_id)
    if workspace.exists() and not reuse_workspace:
        shutil.rmtree(workspace)
    if workspace.exists():
        _run_git(["fetch", "--all", "--tags"], cwd=workspace, timeout=git_timeout_seconds)
        _run_git(["checkout", instance.base_commit], cwd=workspace, timeout=git_timeout_seconds)
        _run_git(["reset", "--hard", instance.base_commit], cwd=workspace, timeout=git_timeout_seconds)
        _run_git(["clean", "-fdx"], cwd=workspace, timeout=git_timeout_seconds)
    else:
        workspace.parent.mkdir(parents=True, exist_ok=True)
        clone_swebench_repo(
            instance.repo,
            workspace,
            repo_cache_root=repo_cache_root,
            retries=clone_retries,
            timeout=git_timeout_seconds,
        )
        _run_git(["checkout", instance.base_commit], cwd=workspace, timeout=git_timeout_seconds)
    _run_git(["config", "user.name", "SWE-bench"], cwd=workspace, timeout=git_timeout_seconds)
    _run_git(["config", "user.email", "swebench@example.invalid"], cwd=workspace, timeout=git_timeout_seconds)
    return workspace


def clone_swebench_repo(
    repo: str,
    workspace: str | Path,
    *,
    repo_cache_root: str | Path | None = DEFAULT_SWEBENCH_REPO_CACHE_ROOT,
    retries: int = 2,
    timeout: int = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> None:
    url = repo_clone_url(repo)
    workspace = Path(workspace)
    cache_root = Path(repo_cache_root) if repo_cache_root is not None else None
    if cache_root is None:
        _run_with_retries(["git", "clone", url, str(workspace)], retries=retries, timeout=timeout)
        return

    mirror = cache_root / (safe_instance_dir(repo) + ".git")
    mirror.parent.mkdir(parents=True, exist_ok=True)
    if mirror.exists():
        _run_with_retries(
            ["git", "remote", "update", "--prune"],
            cwd=mirror,
            retries=retries,
            timeout=timeout,
        )
    else:
        _run_with_retries(
            ["git", "clone", "--mirror", url, str(mirror)],
            retries=retries,
            timeout=timeout,
        )
    _run_with_retries(
        ["git", "clone", str(mirror), str(workspace)],
        retries=retries,
        timeout=timeout,
    )


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
    instance_ids: list[str] | None = None,
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
    ids = [item for item in (instance_ids or []) if item]
    if instance_id:
        ids.append(instance_id)
    if ids:
        command.extend(["--instance_ids", *ids])
    return command


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> None:
    _run_command(["git", *args], cwd=cwd, timeout=timeout)


def _run_with_retries(
    command: list[str],
    *,
    cwd: Path | None = None,
    retries: int = 2,
    timeout: int = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> None:
    attempts = max(1, int(retries or 1))
    errors = []
    for attempt in range(1, attempts + 1):
        try:
            _run_command(command, cwd=cwd, timeout=timeout)
            return
        except RuntimeError as exc:
            errors.append(str(exc))
            if attempt == attempts:
                break
    raise RuntimeError(
        f"Command failed after {attempts} attempt(s): {' '.join(command)}\n"
        + "\n\n".join(errors)
    )


def _run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> None:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout or DEFAULT_GIT_TIMEOUT_SECONDS)),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"command timed out after {timeout}s: {' '.join(command)}\n"
            f"stdout:\n{_preview(exc.stdout)}\n"
            f"stderr:\n{_preview(exc.stderr)}"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(command)}\n"
            f"cwd: {cwd or Path.cwd()}\n"
            f"exit: {result.returncode}\n"
            f"stdout:\n{_preview(result.stdout)}\n"
            f"stderr:\n{_preview(result.stderr)}"
        )


def _preview(value: Any, *, limit: int = 4000) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[-limit:]


def _emit(progress: Callable[[dict[str, Any]], None] | None, event: str, payload: dict[str, Any]) -> None:
    if progress is not None:
        progress({"event": event, **payload})
