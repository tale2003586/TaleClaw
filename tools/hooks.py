
from collections import deque
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from config import WORKDIR
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
    ) -> None:
        self.repeat_limit = max(2, repeat_limit)
        self.tool_repeat_limit = max(2, int(tool_repeat_limit))
        self.window_size = max(self.tool_repeat_limit, window_size)
        self.result_repeat_limit = max(2, int(result_repeat_limit))
        self._recent: dict[str, deque[str]] = {}
        self._recent_tools: dict[str, deque[str]] = {}
        self._result_hash_counts: dict[str, dict[str, int]] = {}

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

    def _fingerprint(self, request: ToolExecutionRequest) -> str:
        return json.dumps(
            {
                "tool": request.tool_name,
                "arguments": request.arguments,
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )


_RESULT_HASH_TOOLS = {
    "list_files",
    "rg",
    "read_file",
    "storage_list_files",
    "storage_read_file",
    "sandbox_list_files",
    "sandbox_read_file",
}


def _output_hash(output: str) -> str:
    normalized = str(output or "")
    if normalized.startswith("[tool-cache] already read at step "):
        normalized = normalized.split("\n", 1)[1] if "\n" in normalized else ""
    normalized = normalized.strip()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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
