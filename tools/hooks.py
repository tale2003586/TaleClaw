
from collections import deque
import re
import time
from pathlib import Path
from typing import Any

from config import WORKDIR
from runtime.tooling.signature import tool_call_signature, tool_result_hash
from tools.executor import HookOutcome, ToolExecutionRequest, ToolExecutionResult, ToolHook


class ShellSafetyHook(ToolHook):
    name = "shell_safety"

    def matches(self, request: ToolExecutionRequest) -> bool:
        return request.tool_name == "bash"

    def before(self, request: ToolExecutionRequest) -> HookOutcome:
        command = str(request.arguments.get("command", ""))
        dangerous_patterns = [
            r"\bsudo\b",
            r"\b(?:shutdown|reboot|halt|poweroff)\b",
            r"\bmkfs(?:\.[a-z0-9]+)?\b",
            r"\bdd\s+.*\bof=/dev/",
            r">\s*/dev/(?:sd|hd|nvme|mapper/)",
            r"\bchmod\s+-?R?\s*777\b",
            r":\s*\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
            r"\brm\s+(?=-[^\s;&|]*r)(?=-[^\s;&|]*f)-[^\s;&|]*\s+(?:/|\.{1,2}|\*|~|\$HOME|/home(?:/|$)|/etc(?:/|$)|/usr(?:/|$)|/var(?:/|$)|/dev(?:/|$))",
        ]
        if any(re.search(pattern, command, re.IGNORECASE) for pattern in dangerous_patterns):
            return HookOutcome(
                deny_reason="Error: Dangerous shell command blocked by shell_safety hook."
            )
        return HookOutcome()


class ShellWorkspaceScopeHook(ToolHook):
    name = "shell_workspace_scope"

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = (workspace or WORKDIR).resolve()

    def matches(self, request: ToolExecutionRequest) -> bool:
        return request.tool_name == "bash"

    def before(self, request: ToolExecutionRequest) -> HookOutcome:
        command = str(request.arguments.get("command", ""))
        metadata = request.metadata or {}
        workspace = (
            Path(str(metadata.get("workspace_root"))).expanduser().resolve()
            if metadata.get("workspace_root")
            else self.workspace
        )
        for raw_target in _absolute_cd_targets(command):
            target = Path(raw_target).expanduser().resolve()
            if not target.is_relative_to(workspace):
                return HookOutcome(
                    deny_reason=(
                        "Error: Shell command changes directory outside workspace. "
                        "bash already runs at the task workspace root; use relative "
                        "paths or cd only within the workspace."
                    )
                )
        return HookOutcome()


class FileWriteScopeHook(ToolHook):
    name = "file_write_scope"
    write_tools = {"write_file", "edit_file"}

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = (workspace or WORKDIR).resolve()

    def matches(self, request: ToolExecutionRequest) -> bool:
        return request.tool_name in self.write_tools

    def before(self, request: ToolExecutionRequest) -> HookOutcome:
        raw_path = request.arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return HookOutcome(deny_reason="Error: Missing file path.")

        metadata = request.metadata or {}
        workspace = (
            Path(str(metadata.get("workspace_root"))).expanduser().resolve()
            if metadata.get("workspace_root")
            else self.workspace
        )
        target = (workspace / raw_path).resolve()
        if not target.is_relative_to(workspace):
            return HookOutcome(
                deny_reason=f"Error: Write path escapes workspace: {raw_path}"
            )
        return HookOutcome()


class ToolLoopGuardHook(ToolHook):
    name = "tool_loop_guard"

    def __init__(
        self,
        repeat_limit: int = 20,
        window_size: int = 6,
        tool_repeat_limit: int = 60,
        result_repeat_limit: int = 3,
        cached_repeat_limit: int = 2,
    ) -> None:
        self.repeat_limit = max(2, repeat_limit)
        self.tool_repeat_limit = max(2, int(tool_repeat_limit))
        self.window_size = max(self.tool_repeat_limit, window_size)
        self.result_repeat_limit = max(2, int(result_repeat_limit))
        self.cached_repeat_limit = max(2, int(cached_repeat_limit))
        self._recent: dict[str, deque[str]] = {}
        self._recent_tools: dict[str, deque[str]] = {}
        self._result_hash_counts: dict[str, dict[str, int]] = {}
        self._result_cache: dict[str, dict[str, str]] = {}
        self._cached_repeat_counts: dict[str, dict[str, int]] = {}

    def matches(self, request: ToolExecutionRequest) -> bool:
        return True

    def before(self, request: ToolExecutionRequest) -> HookOutcome:
        key = request.session_id or "_global"
        fingerprint = self._fingerprint(request)
        recent = self._recent.setdefault(key, deque(maxlen=self.window_size))
        recent_tools = self._recent_tools.setdefault(key, deque(maxlen=self.window_size))
        repeats = sum(1 for item in recent if item == fingerprint)
        tool_repeats = sum(1 for item in recent_tools if item == request.tool_name)
        recent.append(fingerprint)
        recent_tools.append(request.tool_name)

        cached_output = self._cached_output(key, fingerprint, request)
        if cached_output is not None:
            counts = self._cached_repeat_counts.setdefault(key, {})
            count = counts.get(fingerprint, 0) + 1
            counts[fingerprint] = count
            if count >= self.cached_repeat_limit:
                return HookOutcome(
                    deny_reason=(
                        "Error: Repeated cached tool call blocked by "
                        "tool_loop_guard. You already repeated the exact same "
                        "tool call after receiving cached data and a warning. "
                        "Change strategy now: use the cached information, inspect "
                        "a different path/query/offset, or summarize and conclude."
                    )
                )
            return HookOutcome(
                updated_output=self._with_repeat_warning(cached_output)
            )

        if repeats + 1 >= self.repeat_limit:
            return HookOutcome(
                deny_reason=(
                    "Error: Repeated tool call blocked by tool_loop_guard. "
                    "Summarize progress or try a different approach."
                )
            )
        if tool_repeats + 1 >= self.tool_repeat_limit:
            return HookOutcome(
                deny_reason=(
                    "Error: Repeated use of the same tool blocked by tool_loop_guard. "
                    "Summarize progress or choose a different step."
                )
            )
        return HookOutcome()

    def after(
        self,
        request: ToolExecutionRequest,
        result: ToolExecutionResult,
    ) -> HookOutcome | None:
        if request.tool_name not in _RESULT_HASH_TOOLS:
            return None
        if result.status != "success":
            return None
        if request.tool_name in _CACHE_REPLAY_TOOLS:
            self._store_result(request, result.output)
        output_hash = _output_hash(result.output)
        if not output_hash:
            return None
        key = request.session_id or "_global"
        counts = self._result_hash_counts.setdefault(key, {})
        count = counts.get(output_hash, 0) + 1
        counts[output_hash] = count
        if count >= self.result_repeat_limit:
            return HookOutcome(
                deny_reason=(
                    "Error: Repeated no-information-gain tool result blocked by "
                    "tool_loop_guard. You have already received the same read/list "
                    "result multiple times; continue with the offset suggested by "
                    "the previous result, inspect a different path, or summarize "
                    "the available information and provide a conclusion."
                )
            )
        return None

    def reset_turn(self, session_id: str) -> None:
        self._recent.pop(session_id or "_global", None)
        self._recent_tools.pop(session_id or "_global", None)
        self._result_hash_counts.pop(session_id or "_global", None)
        self._result_cache.pop(session_id or "_global", None)
        self._cached_repeat_counts.pop(session_id or "_global", None)

    def _fingerprint(self, request: ToolExecutionRequest) -> str:
        return tool_call_signature(request.tool_name, request.arguments)

    def _cached_output(
        self,
        session_key: str,
        fingerprint: str,
        request: ToolExecutionRequest,
    ) -> str | None:
        if request.tool_name not in _CACHE_REPLAY_TOOLS:
            return None
        cached = self._result_cache.get(session_key, {}).get(fingerprint)
        if cached:
            return cached
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        cache = metadata.get(_TOOL_RESULT_CACHE_KEY)
        if not isinstance(cache, dict):
            return None
        cached_entry = cache.get(fingerprint)
        if not isinstance(cached_entry, dict):
            return None
        output = str(cached_entry.get("output") or "")
        if not output:
            return None
        step = cached_entry.get("step")
        return f"[tool-cache] already read at step {step}; unchanged.\n{output}"

    def _store_result(self, request: ToolExecutionRequest, output: str) -> None:
        if not output or result_is_error(output) or _LOOP_GUARD_WARNING in output:
            return
        key = request.session_id or "_global"
        fingerprint = self._fingerprint(request)
        self._result_cache.setdefault(key, {})[fingerprint] = output

    def _with_repeat_warning(self, output: str) -> str:
        return (
            f"{output.rstrip()}\n\n"
            "[tool-loop-guard] You are repeating the exact same tool call. "
            "This result was replayed from cache instead of executing the tool "
            "again. Change strategy now: use the cached information, inspect a "
            "different path/query/offset, or summarize and conclude. If you "
            "repeat this exact call again, the loop will be stopped."
        )


_RESULT_HASH_TOOLS = {
    "list_files",
    "rg",
    "grep",
    "nl",
    "read_file",
    "read_files",
    "repo_map",
    "code_outline",
    "storage_list_files",
    "storage_read_file",
    "sandbox_list_files",
    "sandbox_read_file",
}


_CACHE_REPLAY_TOOLS = set(_RESULT_HASH_TOOLS)
_TOOL_RESULT_CACHE_KEY = "_tool_result_cache"
_LOOP_GUARD_WARNING = "[tool-loop-guard]"


def result_is_error(output: str) -> bool:
    return str(output or "").lstrip().startswith("Error:")


def _output_hash(output: str) -> str:
    return tool_result_hash(output)


class ToolTraceHook(ToolHook):
    name = "tool_trace"

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def matches(self, request: ToolExecutionRequest) -> bool:
        return True

    def after(self, request: ToolExecutionRequest, result: ToolExecutionResult) -> None:
        self.records.append({
            "timestamp": time.time(),
            "session_id": request.session_id,
            "source": request.source,
            "call_id": request.call_id,
            "tool_name": request.tool_name,
            "status": result.status,
            "final_arguments": result.final_arguments,
            "result_preview": str(result.output)[:500],
        })


class ToolResultStoreHook(ToolHook):
    name = "tool_result_store"

    def __init__(
        self,
        *,
        min_chars: int = 1000,
        skip_tools: set[str] | None = None,
    ) -> None:
        self.min_chars = max(0, int(min_chars))
        self.skip_tools = set(skip_tools or {"retrieve_tool_result"})

    def matches(self, request: ToolExecutionRequest) -> bool:
        return request.tool_name not in self.skip_tools

    def after(self, request: ToolExecutionRequest, result: ToolExecutionResult) -> None:
        output = str(result.output or "")
        if len(output) < self.min_chars:
            return None
        if _LOOP_GUARD_WARNING in output:
            return None
        if output.startswith("[tool-cache] already read at step "):
            return None
        try:
            from runtime.tooling.result_store import (
                TOOL_RESULT_STORE_REF_KEY,
                store_tool_result,
            )

            stored = store_tool_result(
                session_id=request.session_id,
                call_id=request.call_id,
                tool_name=request.tool_name,
                arguments=result.final_arguments,
                content=output,
                status=result.status,
            )
            result.metadata[TOOL_RESULT_STORE_REF_KEY] = {
                "result_id": stored.result_id,
                "backend": stored.backend,
                "uri": stored.uri,
                "chars": stored.chars,
                "sha256": stored.sha256,
            }
        except Exception as exc:
            result.metadata["tool_result_store_error"] = str(exc)
        return None


def _absolute_cd_targets(command: str) -> list[str]:
    targets = []
    pattern = re.compile(
        r"(?:^|[;&|]\s*)cd\s+(?P<quote>['\"]?)(?P<target>/[^'\";&|`\s]*)(?P=quote)"
    )
    for match in pattern.finditer(command or ""):
        target = match.group("target").strip()
        if target:
            targets.append(target)
    return targets
