"""Bounded local shell execution; process exit is separate from task success.

The managed process group is a cancellation unit, not a filesystem/network
sandbox. A command that deliberately creates a new session can escape it.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
import signal
import subprocess
import threading
import time
from uuid import uuid4
from typing import Callable

from tools.executor import ToolHandlerOutput, tool_error


def _single_pytest_command(command: str) -> bool:
    """Recognize a test invocation, never a compound shell program.

    This grants permission to inspect a completed failing test result, not a
    claim that tests are pure or that retrying a side effect is safe.
    """
    if not isinstance(command, str) or any(char in command for char in ";&|<>`$\n\r()"):
        return False
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    if not args:
        return False
    executable = Path(args[0]).name
    return executable in {"pytest", "py.test"} or (
        re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)?)?", executable) is not None
        and args[1:3] == ["-m", "pytest"]
    )


def completed_diagnostic_failure(result) -> bool:
    """A normal pytest exit 1 may return to reasoning with effects still unknown.

    Timeouts, cancellation, signals, unfinished children, incomplete output and
    arbitrary failed writes remain stop conditions. Status stays error; this is
    neither task verification nor an automatic retry authorization.
    """
    metadata = getattr(result, "metadata", {}) or {}
    return bool(
        getattr(result, "status", None) == "error"
        and getattr(result, "error_type", None) == "CommandExitError"
        and type(getattr(result, "exit_code", None)) is int and result.exit_code == 1
        and getattr(result, "effect_state", None) == "unknown"
        and metadata.get("diagnostic_failure") == "pytest_tests_failed"
        and metadata.get("process_exited") is True
        and metadata.get("process_group_exited") is True
        and metadata.get("output_complete") is True
        and metadata.get("managed_descendants_exited", True) is True
        and _single_pytest_command(metadata.get("command"))
    )


def _group_running(process: subprocess.Popen) -> bool:
    if os.name != "posix":
        return process.poll() is None
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    # Orphaned zombies cannot execute further effects and need not block exit
    # confirmation while the container's PID 1 waits to reap them.
    if Path("/proc").is_dir():
        observed = False
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) == process.pid:
                    observed = True
                    if fields[0] != "Z":
                        return True
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (PermissionError, ValueError, IndexError):
                # Do not turn an unreadable process table into proof of exit.
                return True
        if observed:
            return False
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    return True


def _stop_group(process: subprocess.Popen, grace_seconds: float) -> bool:
    def send(sig: int) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, sig)
            elif process.poll() is None:
                process.terminate() if sig == signal.SIGTERM else process.kill()
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    until = time.monotonic() + grace_seconds
    while _group_running(process) and time.monotonic() < until:
        process.poll()
        time.sleep(0.01)
    if _group_running(process):
        send(signal.SIGKILL)
    try:
        process.wait(timeout=max(0.1, grace_seconds))
    except subprocess.TimeoutExpired:
        return False
    until = time.monotonic() + grace_seconds
    while _group_running(process) and time.monotonic() < until:
        time.sleep(0.01)
    return not _group_running(process)


def _process_baseline() -> dict[int, str]:
    """Exclude processes that existed before launch, including nondumpable peers."""
    result = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid == os.getuid():
                result[int(path.name)] = (path / "stat").read_text().rsplit(")", 1)[1].split()[19]
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, IndexError) as exc:
            raise RuntimeError("Cannot establish managed process baseline") from exc
    return result


def _tagged_children(tag: str, baseline: dict[int, str] | None = None) -> list[int]:
    result = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            # Other users cannot inherit our child environment. An unreadable
            # process owned by this user, however, is not proof of exit.
            if path.stat().st_uid != os.getuid():
                continue
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                continue
            if baseline is not None and baseline.get(int(path.name)) == fields[19]:
                continue
            environment = (path / "environ").read_bytes().split(b"\0")
            if ("TALECLAW_COMMAND_ID=" + tag).encode() in environment:
                result.append(int(path.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, IndexError) as exc:
            raise RuntimeError("Cannot confirm managed descendants from the process table") from exc
    return result


def _stop_tagged_children(tag: str, grace: float, baseline: dict[int, str]) -> bool:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in _tagged_children(tag, baseline):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        until = time.monotonic() + max(grace, 0.1)
        while _tagged_children(tag, baseline) and time.monotonic() < until:
            time.sleep(0.01)
    return not _tagged_children(tag, baseline)


def run_command(command: str, *, cwd: str | Path, managed_binding: dict | None = None, **options) -> ToolHandlerOutput:
    if managed_binding is None:
        return _run_command(command, cwd=cwd, **options)
    from runtime.worktrees import WorktreeService
    from runtime.workspace_lease import WorkspaceBusyError
    service = WorktreeService(Path(managed_binding["service_root"]))
    binding = service.inspect(managed_binding["worktree_id"], owner_id=managed_binding["owner_id"])
    if Path(cwd).resolve() != Path(binding["workspace_root"]):
        raise ValueError("Managed command directory does not match its registered workspace")
    if os.name != "posix" or not Path("/proc").is_dir():
        raise RuntimeError("Managed commands require Linux process inspection")
    try:
        return _run_managed_command(command, cwd=cwd, service=service, binding=binding, options=options)
    except WorkspaceBusyError:
        return tool_error("Error: Managed command resources are busy; retry after the active command exits",
                          error_type="ResourceBusy", effect_state="not_started")


def _run_managed_command(command, *, cwd, service, binding, options):
    # Include environment preparation in the command deadline. A busy lease
    # never blocks and never invokes the command.
    timeout = options.get("timeout_seconds", 120)
    deadline = options.get("deadline")
    options = {**options, "deadline": min(time.monotonic() + timeout, deadline if deadline is not None else float("inf"))}
    with service.command_scope(binding) as (environment, completion):
        try:
            baseline = _process_baseline()
        except RuntimeError:
            completion["confirmed"] = True
            return tool_error("Error: Cannot establish process visibility before managed command start",
                              error_type="CommandInspectionUnavailable", effect_state="not_started")
        tag = uuid4().hex
        environment["TALECLAW_COMMAND_ID"] = tag
        version = subprocess.run(
            [str(Path(environment["VIRTUAL_ENV"]) / "bin" / "python"), "-I", "-S", "-c",
             "import sys; print(sys.version.split()[0])"], env=environment, capture_output=True,
            text=True, timeout=5, check=True,
        ).stdout.strip()
        result = _run_command(command, cwd=cwd, environment=environment, **options)
        result.metadata["managed_environment"] = {
            "worktree_id": binding["worktree_id"], "workspace_root": str(cwd),
            "virtual_env": environment["VIRTUAL_ENV"], "tmpdir": environment["TMPDIR"],
            "port": environment["PORT"], "compose_project_name": environment["COMPOSE_PROJECT_NAME"],
            "python_version": version,
        }
        try:
            remaining = _tagged_children(tag, baseline)
            descendants_exited = _stop_tagged_children(tag, options.get("termination_grace_seconds", 0.2), baseline) if remaining else True
        except RuntimeError:
            return ToolHandlerOutput(
                "Error: Managed command descendants could not be inspected\n" + str(result),
                status="unknown", error_type="CommandExitUnconfirmed", effect_state="unknown",
                metadata={**result.metadata, "managed_descendants_exited": False},
            )
        completion["confirmed"] = descendants_exited and (
            result.effect_state == "not_started" or (
                result.metadata.get("process_group_exited") is True and
                result.metadata.get("output_complete") is True))
        if remaining:
            return ToolHandlerOutput(
                "Error: Managed command left descendants; cleanup was " + ("confirmed" if completion["confirmed"] else "unconfirmed") + "\n" + str(result),
                status="error" if completion["confirmed"] else "unknown", error_type="UnfinishedCommandChildren",
                effect_state="unknown", metadata={**result.metadata, "managed_descendants_exited": descendants_exited},
            )
        return result


def _run_command(
    command: str, *, cwd: str | Path, timeout_seconds: float = 120,
    cancel_requested: Callable[[], bool] | None = None,
    deadline: float | None = None, max_output_bytes: int = 50_000,
    termination_grace_seconds: float = 0.2,
    environment: dict[str, str] | None = None,
) -> ToolHandlerOutput:
    """Execute once. Nonzero exit, timeout and cancellation stay distinguishable.

    Output is drained continuously but its in-memory preview is bounded. No
    full-output artifact is claimed when truncation occurs. Side effects remain
    unknown even when the process exits normally.
    """
    if timeout_seconds <= 0 or max_output_bytes < 1 or termination_grace_seconds < 0:
        raise ValueError("Command bounds must be positive")
    started = time.monotonic()
    limit = min(started + timeout_seconds, deadline) if deadline is not None else started + timeout_seconds
    try:
        if cancel_requested is not None and cancel_requested():
            return tool_error("Error: Command cancelled before start", status="cancelled",
                              error_type="CommandCancelled", effect_state="not_started")
    except Exception as exc:
        return tool_error(f"Error: Cancellation check failed: {exc}",
                          error_type="CancellationCheckError", effect_state="not_started")
    if started >= limit:
        return tool_error("Error: Command deadline expired before start", status="timeout",
                          error_type="CommandTimeout", effect_state="not_started")
    try:
        process = subprocess.Popen(
            command, shell=True, cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=(os.name == "posix"), env=environment,
        )
    except (OSError, ValueError) as exc:
        return tool_error(f"Error: {type(exc).__name__}: {exc}",
                          error_type=type(exc).__name__, effect_state="not_started")

    preview = bytearray()
    observed_bytes = 0
    read_errors: list[str] = []

    def drain() -> None:
        nonlocal observed_bytes
        try:
            assert process.stdout is not None
            while chunk := process.stdout.read(8192):
                observed_bytes += len(chunk)
                remaining = max_output_bytes - len(preview)
                if remaining > 0:
                    preview.extend(chunk[:remaining])
        except (OSError, ValueError) as exc:
            read_errors.append(f"{type(exc).__name__}: {exc}")

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    status, error_type, error_message = "success", None, None
    group_exited = False
    try:
        while process.poll() is None:
            try:
                cancelled = cancel_requested is not None and cancel_requested()
            except Exception as exc:
                status, error_type, error_message = "error", "CancellationCheckError", str(exc)
                break
            if cancelled:
                status, error_type, error_message = "cancelled", "CommandCancelled", "Command cancelled"
                break
            if time.monotonic() >= limit:
                status, error_type, error_message = "timeout", "CommandTimeout", "Command execution timed out"
                break
            time.sleep(min(0.02, max(0, limit - time.monotonic())))
        if status != "success":
            group_exited = _stop_group(process, termination_grace_seconds)
        elif _group_running(process):
            # Background descendants must not keep mutating after a synchronous
            # shell call has returned. Background execution needs its own owner.
            status, error_type = "error", "UnfinishedCommandChildren"
            error_message = "Shell exited while command descendants were still running"
            group_exited = _stop_group(process, termination_grace_seconds)
        else:
            group_exited = True
    finally:
        if process.poll() is None:
            group_exited = _stop_group(process, termination_grace_seconds)
        reader.join(timeout=max(0.1, termination_grace_seconds))
        if not reader.is_alive() and process.stdout is not None:
            process.stdout.close()

    exit_code = process.poll()
    if not group_exited or reader.is_alive():
        status, error_type = "unknown", "CommandExitUnconfirmed"
        error_message = "Managed command exit/output completion could not be confirmed"
    elif read_errors:
        status, error_type = "error", "CommandOutputError"
        error_message = "; ".join(read_errors)
    elif status == "success" and exit_code != 0:
        status, error_type = "error", "CommandExitError"
        error_message = f"Command exited with code {exit_code}"
    output = bytes(preview).decode("utf-8", errors="replace").strip() or "(no output)"
    if error_message:
        output = f"Error: {error_message}\n{output}"
    return ToolHandlerOutput(
        output, status=status, error_type=error_type, error_message=error_message,
        exit_code=exit_code, effect_state="unknown",
        metadata={
            "command": command, "cwd": str(cwd),
            "process_exited": exit_code is not None,
            "process_group_exited": group_exited,
            "output_complete": not reader.is_alive() and not read_errors,
            "output_truncated": observed_bytes > max_output_bytes,
            "observed_output_bytes": observed_bytes,
            **({"diagnostic_failure": "pytest_tests_failed"} if (
                status == "error" and error_type == "CommandExitError" and exit_code == 1
                and group_exited and not reader.is_alive() and not read_errors
                and _single_pytest_command(command)
            ) else {}),
        },
    )
