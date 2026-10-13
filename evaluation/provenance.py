"""Small, explicit evaluation provenance; never snapshots the process environment."""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOURCE_PATHS = (
    "config.py", "agents/definitions.py", "applications/coding/runner.py",
    "runtime/runtime.py", "runtime/execution/reasoning_loop.py", "runtime/execution/budget.py",
    "runtime/trace/trace_store.py", "models/provider.py", "models/model_pool.py",
    "tools/executor.py", "tools/tool_registry.py", "evaluation/harness.py",
    "evaluation/verifiers.py", "evaluation/outcomes.py", "evaluation/provenance.py",
    "evaluation/swebench_adapter.py", "scripts/run_swebench_verified.py",
    "scripts/run_swebench_verified_matrix.py",
    "runtime/context/policies.py", "runtime/context/snapshots.py", "runtime/context/builder.py",
    "runtime/context/dynamic_budget.py", "runtime/context/native_policy.py",
    "runtime/trace/context_intervention.py", "models/context_state.py",
    "applications/coding/context_state.py", "applications/coding/task_state.py",
    "applications/coding/reasoning_memory.py", "applications/coding/evidence_validity.py",
    "applications/coding/state_updates.py", "tools/schema.py", "tools/handlers.py",
    "evaluation/context_experiments.py", "evaluation/session_manager.py",
    "evaluation/context_pressure.py", "scripts/run_context_policy_trial.py",
)
CONFIG_FIELDS = frozenset({
    "runner_mode", "model_name", "model_purpose", "max_reasoning_steps", "no_step_budget",
    "dataset_name", "dataset_split", "selected_ids", "limit", "offset", "evaluate",
    "reuse_workspace", "clone_retries", "git_timeout_seconds", "official_max_workers",
    "cell_ids", "instance_id", "target_repo", "target_base_commit",
    "policies", "context_policy", "execution_limits", "execution_budget_limits",
    "repeat", "seed", "task_id",
})


def content_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _file_digest(path: Path) -> dict[str, Any]:
    # Never follow aliases to configuration secrets or ingest an environment file.
    if path.is_symlink() or path.name == ".env" or path.name.startswith(".env."):
        return {"status": "excluded"}
    try:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        return {"status": "available", "sha256": digest.hexdigest(), "bytes": size}
    except OSError as exc:
        return {"status": "unavailable", "error_type": type(exc).__name__}


def _git_state(root: Path) -> dict[str, Any]:
    def git(*args):
        return subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), *args],
            capture_output=True, check=True, timeout=5,
        ).stdout
    try:
        head = git("rev-parse", "--verify", "HEAD").decode().strip()
        status = git("status", "--porcelain=v1", "--untracked-files=normal")
        return {"status": "available", "head": head, "dirty": bool(status),
                "changed_entries": len(status.splitlines()),
                "status_sha256": hashlib.sha256(status).hexdigest()}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unavailable", "head": None, "dirty": None, "error_type": type(exc).__name__}


def evaluation_manifest(
    *, repo_root: Path | None = None, dataset_path: Path | None = None,
    configuration: dict[str, Any] | None = None, source_paths: tuple[str, ...] = SOURCE_PATHS,
    input_records: Any = None,
) -> dict[str, Any]:
    root = Path(repo_root or Path(__file__).resolve().parents[1])
    config = {key: value for key, value in (configuration or {}).items() if key in CONFIG_FIELDS}
    sources = {}
    for relative in source_paths:
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("Source provenance paths must stay within the project.")
        sources[relative] = _file_digest(root / candidate)
    result = {
        "schema_version": "evaluation_manifest.v1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(), "code_state": _git_state(root),
        "source_files": sources, "source_set_sha256": content_digest(sources),
        "configuration": config, "configuration_sha256": content_digest(config),
        "scope": "Explicit source subset and inputs; not a full dependency, environment, prompt, or fixture lock. No credentials or process environment captured.",
    }
    if dataset_path is not None:
        result["dataset_file"] = _file_digest(Path(dataset_path))
        fixtures = _fixture_digests(Path(dataset_path), selected_ids=config.get("selected_ids"))
        if fixtures is not None:
            result["fixtures"] = fixtures
            result["fixtures_sha256"] = content_digest(fixtures)
    if input_records is not None:
        result["input_records_sha256"] = content_digest(input_records)
    return result


def _fixture_digests(dataset_path: Path, *, selected_ids=None) -> dict | None:
    """Lock declared coding fixture bytes, excluding credentials and generated state."""
    if dataset_path.is_symlink() or dataset_path.name == ".env" or dataset_path.name.startswith(".env."):
        return None
    try:
        payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list) or not any(isinstance(task, dict) and task.get("fixture_repo") for task in tasks):
        return None
    fixture_root = dataset_path.resolve().parent.parent
    excluded_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}
    records = {}
    for task in tasks:
        if not isinstance(task, dict) or not task.get("fixture_repo") or selected_ids and task.get("id") not in selected_ids:
            continue
        identifier = str(task.get("id") or task["fixture_repo"])
        relative = Path(str(task["fixture_repo"]))
        if relative.is_absolute() or ".." in relative.parts:
            records[identifier] = {"status": "excluded", "reason": "outside_fixture_root"}
            continue
        directory = fixture_root / relative
        if directory.is_symlink() or not directory.is_dir() or not directory.resolve().is_relative_to(fixture_root):
            records[identifier] = {"status": "unavailable", "reason": "missing_or_aliased_fixture"}
            continue
        files = {}
        for path in sorted(directory.rglob("*")):
            name = path.relative_to(directory).as_posix()
            if any(part in excluded_dirs for part in path.relative_to(directory).parts):
                continue
            if path.is_symlink():
                files[name] = {"status": "excluded", "reason": "symlink"}
            elif path.is_file() and path.suffix != ".pyc":
                files[name] = _file_digest(path)
        records[identifier] = {"status": "available", "fixture_repo": relative.as_posix(), "files": files}
    return records
