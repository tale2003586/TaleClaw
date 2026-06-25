#!/usr/bin/env python3
"""Small local smoke-set runner for coding agents.

The runner is intentionally dependency-free. It can:
- list tasks from tasks.json
- materialize isolated git workspaces for each task
- optionally run an agent command against each workspace
- run task checks and write a JSON result report

Example:
    python evaluation/coding_agent_smoke/run_smoke.py init --out /tmp/agent-smoke
    python evaluation/coding_agent_smoke/run_smoke.py check --workdir /tmp/agent-smoke
    python evaluation/coding_agent_smoke/run_smoke.py run \
      --out /tmp/agent-smoke \
      --agent-cmd 'your-agent --cwd {case_dir} --prompt-file {prompt_file}'
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
TASKS_PATH = ROOT / "tasks.json"


class CheckError(AssertionError):
    pass


def load_tasks(path: Path = TASKS_PATH) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    tasks = data.get("tasks", data)
    if not isinstance(tasks, list):
        raise ValueError("tasks.json must contain a top-level task list or {'tasks': [...]}")
    return tasks


def select_tasks(tasks: list[dict[str, Any]], selected: str | None) -> list[dict[str, Any]]:
    if not selected:
        return tasks
    wanted = {item.strip() for item in selected.split(",") if item.strip()}
    chosen = [task for task in tasks if task["id"] in wanted]
    missing = wanted - {task["id"] for task in chosen}
    if missing:
        raise SystemExit(f"Unknown task id(s): {', '.join(sorted(missing))}")
    return chosen


def as_text(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(str(line) for line in value) + "\n"
    if isinstance(value, str):
        return value
    raise TypeError(f"Expected string or list of lines, got {type(value).__name__}")


def write_text_file(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(as_text(value), encoding="utf-8")


def write_binary_file(path: Path, encoded: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(encoded))


def apply_modes(case_dir: Path, modes: dict[str, str]) -> None:
    for rel, mode_text in modes.items():
        path = case_dir / rel
        mode = int(mode_text, 8)
        path.chmod(mode)


def run_command(
    cmd: str,
    cwd: Path,
    timeout: int = 30,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        shell=True,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    if check and result.returncode != 0:
        raise CheckError(
            f"command failed: {cmd}\nexit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def init_git(case_dir: Path) -> None:
    run_command("git init -q", case_dir, check=True)
    run_command("git config user.email smoke@example.local", case_dir, check=True)
    run_command("git config user.name Smoke Runner", case_dir, check=True)
    run_command("git add .", case_dir, check=True)
    run_command("git commit -qm initial-fixture", case_dir, check=True)


def write_prompt(case_dir: Path, task: dict[str, Any]) -> None:
    lines = [
        f"# {task['id']} - {task.get('title', '')}",
        "",
        task["prompt"].rstrip(),
        "",
    ]
    commands = task.get("validation_commands", [])
    if commands:
        lines.extend(["Validation commands:", ""])
        lines.extend(f"- `{cmd}`" for cmd in commands)
        lines.append("")
    write_text_file(case_dir / "AGENT_TASK.md", "\n".join(lines))


def materialize_task(task: dict[str, Any], out_dir: Path, force: bool = False) -> Path:
    case_dir = out_dir / task["id"]
    if case_dir.exists():
        if not force:
            raise SystemExit(f"{case_dir} already exists; pass --force to recreate it")
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)

    for rel, value in task.get("files", {}).items():
        write_text_file(case_dir / rel, value)
    for rel, encoded in task.get("binary_files", {}).items():
        write_binary_file(case_dir / rel, encoded)
    apply_modes(case_dir, task.get("modes", {}))
    write_prompt(case_dir, task)

    init_git(case_dir)

    for rel, value in task.get("dirty_files", {}).items():
        write_text_file(case_dir / rel, value)
    apply_modes(case_dir, task.get("dirty_modes", {}))
    return case_dir


def path_for(case_dir: Path, rel: str) -> Path:
    path = (case_dir / rel).resolve()
    case_root = case_dir.resolve()
    try:
        path.relative_to(case_root)
    except ValueError as exc:
        raise CheckError(f"path escapes case dir: {rel}") from exc
    return path


def read_json_value(path: Path, dotted_key: str) -> Any:
    with path.open("r", encoding="utf-8") as f:
        current: Any = json.load(f)
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            raise CheckError(f"missing JSON key {dotted_key!r} in {path}")
        current = current[part]
    return current


def changed_paths(case_dir: Path) -> list[str]:
    result = run_command("git status --porcelain=v1 -z --untracked-files=all", case_dir, check=True)
    paths: set[str] = set()
    entries = result.stdout.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:]
        if status[0] in {"R", "C"} or status[1] in {"R", "C"}:
            paths.add(path)
            if index < len(entries) and entries[index]:
                paths.add(entries[index])
                index += 1
        else:
            paths.add(path)
    return sorted(path for path in paths if not is_ignored_generated_path(path))


def is_ignored_generated_path(path: str) -> bool:
    parts = Path(path).parts
    return (
        "__pycache__" in parts
        or path.endswith(".pyc")
        or path.startswith(".pytest_cache/")
        or path.startswith(".mypy_cache/")
        or path.startswith(".ruff_cache/")
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_one(task: dict[str, Any], case_dir: Path, check_def: dict[str, Any]) -> None:
    kind = check_def["type"]

    if kind == "file_exists":
        rel = check_def["path"]
        if not path_for(case_dir, rel).exists():
            raise CheckError(f"expected file to exist: {rel}")
        return

    if kind == "file_not_exists":
        rel = check_def["path"]
        if path_for(case_dir, rel).exists():
            raise CheckError(f"expected file to be absent: {rel}")
        return

    if kind == "file_equals":
        rel = check_def["path"]
        expected = as_text(check_def["text"])
        actual = path_for(case_dir, rel).read_text(encoding="utf-8")
        if actual != expected:
            raise CheckError(f"{rel} did not match expected content")
        return

    if kind == "file_contains":
        rel = check_def["path"]
        text = check_def["text"]
        actual = path_for(case_dir, rel).read_text(encoding="utf-8")
        if text not in actual:
            raise CheckError(f"{rel} does not contain {text!r}")
        return

    if kind == "file_not_contains":
        rel = check_def["path"]
        text = check_def["text"]
        actual = path_for(case_dir, rel).read_text(encoding="utf-8")
        if text in actual:
            raise CheckError(f"{rel} unexpectedly contains {text!r}")
        return

    if kind == "json_value":
        rel = check_def["path"]
        actual = read_json_value(path_for(case_dir, rel), check_def["key"])
        expected = check_def["equals"]
        if actual != expected:
            raise CheckError(f"{rel}:{check_def['key']} expected {expected!r}, got {actual!r}")
        return

    if kind == "command":
        timeout = int(check_def.get("timeout", 30))
        result = run_command(check_def["cmd"], case_dir, timeout=timeout)
        if result.returncode != int(check_def.get("exit_code", 0)):
            raise CheckError(
                f"command failed: {check_def['cmd']}\nexit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        if "stdout_contains" in check_def and check_def["stdout_contains"] not in result.stdout:
            raise CheckError(f"stdout does not contain {check_def['stdout_contains']!r}: {result.stdout!r}")
        if "stdout_equals" in check_def and result.stdout.strip() != check_def["stdout_equals"]:
            raise CheckError(f"stdout expected {check_def['stdout_equals']!r}, got {result.stdout.strip()!r}")
        return

    if kind == "only_changed":
        expected = sorted(check_def["paths"])
        actual = changed_paths(case_dir)
        if actual != expected:
            raise CheckError(f"changed paths expected {expected}, got {actual}")
        return

    if kind == "mode_executable":
        rel = check_def["path"]
        mode = path_for(case_dir, rel).stat().st_mode
        if not (mode & stat.S_IXUSR):
            raise CheckError(f"expected executable bit on {rel}")
        return

    if kind == "binary_unchanged":
        rel = check_def["path"]
        encoded = task.get("binary_files", {}).get(rel)
        if encoded is None:
            raise CheckError(f"no fixture binary registered for {rel}")
        expected = sha256_bytes(base64.b64decode(encoded))
        actual = sha256_bytes(path_for(case_dir, rel).read_bytes())
        if actual != expected:
            raise CheckError(f"binary changed: {rel}")
        return

    raise CheckError(f"unknown check type: {kind}")


def run_checks(task: dict[str, Any], case_dir: Path) -> dict[str, Any]:
    started = time.monotonic()
    failures: list[str] = []

    for cmd in task.get("validation_commands", []):
        try:
            run_command(cmd, case_dir, timeout=int(task.get("validation_timeout", 30)), check=True)
        except Exception as exc:  # noqa: BLE001 - report all check failures
            failures.append(str(exc))

    for check_def in task.get("checks", []):
        try:
            check_one(task, case_dir, check_def)
        except Exception as exc:  # noqa: BLE001 - report all check failures
            failures.append(str(exc))

    return {
        "id": task["id"],
        "title": task.get("title", ""),
        "passed": not failures,
        "failures": failures,
        "duration_sec": round(time.monotonic() - started, 3),
    }


def format_agent_cmd(template: str, task: dict[str, Any], case_dir: Path) -> str:
    values = {
        "case_dir": shlex.quote(str(case_dir)),
        "prompt_file": shlex.quote(str(case_dir / "AGENT_TASK.md")),
        "task_id": shlex.quote(task["id"]),
        "prompt": shlex.quote(task["prompt"]),
    }
    return template.format(**values)


def cmd_list(args: argparse.Namespace) -> int:
    tasks = select_tasks(load_tasks(), args.tasks)
    for task in tasks:
        print(f"{task['id']}\t{task.get('category', '-')}\t{task.get('title', '')}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    tasks = select_tasks(load_tasks(), args.tasks)
    out_dir.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        case_dir = materialize_task(task, out_dir, force=args.force)
        print(f"created {task['id']}: {case_dir}")
    print(f"\nNext: run your agent in each case dir, then run:")
    print(f"  python {Path(__file__).resolve()} check --workdir {out_dir}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    tasks = select_tasks(load_tasks(), args.tasks)
    results = []
    for task in tasks:
        case_dir = workdir / task["id"]
        if not case_dir.exists():
            results.append(
                {
                    "id": task["id"],
                    "title": task.get("title", ""),
                    "passed": False,
                    "failures": [f"missing case directory: {case_dir}"],
                    "duration_sec": 0,
                }
            )
            continue
        result = run_checks(task, case_dir)
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"{status} {task['id']} {task.get('title', '')}")
        for failure in result["failures"]:
            print(f"  - {failure.splitlines()[0]}")

    summary = {
        "total": len(results),
        "passed": sum(1 for result in results if result["passed"]),
        "failed": sum(1 for result in results if not result["passed"]),
        "results": results,
    }
    if args.json_output:
        Path(args.json_output).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nSummary: {summary['passed']}/{summary['total']} passed")
    return 0 if summary["failed"] == 0 else 1


def cmd_run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    tasks = select_tasks(load_tasks(), args.tasks)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for task in tasks:
        case_dir = materialize_task(task, out_dir, force=args.force)
        prompt_file = case_dir / "AGENT_TASK.md"
        agent_cmd = format_agent_cmd(args.agent_cmd, task, case_dir)
        print(f"\n=== {task['id']} {task.get('title', '')} ===")
        print(f"prompt: {prompt_file}")
        started = time.monotonic()
        agent_result: dict[str, Any]
        try:
            completed = run_command(agent_cmd, case_dir, timeout=args.agent_timeout)
            agent_result = {
                "exit_code": completed.returncode,
                "duration_sec": round(time.monotonic() - started, 3),
                "stdout": completed.stdout[-4000:],
                "stderr": completed.stderr[-4000:],
            }
        except subprocess.TimeoutExpired as exc:
            agent_result = {
                "exit_code": None,
                "duration_sec": round(time.monotonic() - started, 3),
                "stdout": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
                "stderr": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
                "timeout": True,
            }

        check_result = run_checks(task, case_dir)
        check_result["agent"] = agent_result
        results.append(check_result)
        status = "PASS" if check_result["passed"] else "FAIL"
        print(f"{status} {task['id']}")
        if not check_result["passed"] and not args.keep_going:
            break

    summary = {
        "total": len(results),
        "passed": sum(1 for result in results if result["passed"]),
        "failed": sum(1 for result in results if not result["passed"]),
        "results": results,
    }
    output_path = out_dir / "results.json"
    output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nSummary: {summary['passed']}/{summary['total']} passed")
    print(f"Results: {output_path}")
    return 0 if summary["failed"] == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run local coding-agent smoke tasks.")
    parser.add_argument("--tasks-file", default=str(TASKS_PATH), help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="List available tasks")
    list_parser.add_argument("--tasks", help="Comma-separated task ids")
    list_parser.set_defaults(func=cmd_list)

    init_parser = sub.add_parser("init", help="Create task workspaces")
    init_parser.add_argument("--out", required=True, help="Output directory for task workspaces")
    init_parser.add_argument("--tasks", help="Comma-separated task ids")
    init_parser.add_argument("--force", action="store_true", help="Recreate existing task directories")
    init_parser.set_defaults(func=cmd_init)

    check_parser = sub.add_parser("check", help="Check existing task workspaces")
    check_parser.add_argument("--workdir", required=True, help="Directory created by init/run")
    check_parser.add_argument("--tasks", help="Comma-separated task ids")
    check_parser.add_argument("--json-output", help="Optional JSON report path")
    check_parser.set_defaults(func=cmd_check)

    run_parser = sub.add_parser("run", help="Create workspaces, run an agent command, then check")
    run_parser.add_argument("--out", required=True, help="Output directory for task workspaces")
    run_parser.add_argument(
        "--agent-cmd",
        required=True,
        help="Shell command template. Placeholders: {case_dir}, {prompt_file}, {task_id}, {prompt}",
    )
    run_parser.add_argument("--tasks", help="Comma-separated task ids")
    run_parser.add_argument("--force", action="store_true", help="Recreate existing task directories")
    run_parser.add_argument("--keep-going", action="store_true", help="Continue after failed tasks")
    run_parser.add_argument("--agent-timeout", type=int, default=300, help="Per-task agent timeout in seconds")
    run_parser.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    global TASKS_PATH
    TASKS_PATH = Path(args.tasks_file).resolve()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
