from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ALWAYS_ON_TOOLS = {
    "recall_memory",
    "memorize",
    "tool_search",
}

PRELOADED_TOOLS_BY_MODE = {
    "bot": {
        "load_skill",
        "storage_list_files",
        "storage_read_file",
        "storage_write_file",
        "sandbox_list_files",
        "sandbox_read_file",
        "sandbox_write_file",
        "publish_artifact",
    },
    "coding": {
        "bash",
        "edit_file",
        "list_files",
        "rg",
        "grep",
        "nl",
        "repo_map",
        "code_outline",
        "read_file",
        "write_file",
        "git_status",
        "git_diff",
        "git_log",
        "git_branch",
        "load_skill",
        "task_create",
        "task_update",
        "task_list",
        "task_get",
        "task",
        "parallel_tasks",
        "claim_task",
        "check_background",
        "read_inbox",
        "compact",
    },
    "teammate": {
        "list_files",
        "rg",
        "grep",
        "nl",
        "repo_map",
        "code_outline",
        "read_file",
        "git_status",
        "git_diff",
        "git_log",
        "git_branch",
        "load_skill",
        "task_create",
        "task_update",
        "task_list",
        "task_get",
        "claim_task",
        "check_background",
        "send_message",
        "read_inbox",
        "idle",
        "shutdown_response",
        "plan_approval_request",
    },
}

DEFERRED_TOOLS = {
    "bash",
    "write_file",
    "edit_file",
    "background_run",
    "git_add",
    "git_commit",
    "spawn_teammate",
    "list_teammates",
    "broadcast",
    "shutdown_request",
    "shutdown_status",
    "plan_approval",
    "claim_task",
}

UNLOCKED_TOOLS_KEY = "unlocked_tools"


@dataclass(frozen=True)
class ToolPolicyDecision:
    allowed: bool
    reason: str = ""
    requires_approval: bool = False


class ToolPolicy:
    """Runtime policy for tool visibility and execution decisions."""

    def __init__(self, registry) -> None:
        self.registry = registry

    def visible_tools(
        self,
        session,
        mode: str = "coding",
        run_context: Any | None = None,
    ) -> set[str]:
        allowed = self._allowed_names(session=session, mode=mode)
        metadata = getattr(session, "metadata", {}) or {}

        unlocked = set(metadata.get(UNLOCKED_TOOLS_KEY, []))
        visible = (
            ALWAYS_ON_TOOLS
            | {
                name
                for name, tool in self.registry._tools.items()
                if tool.always_on
            }
            | PRELOADED_TOOLS_BY_MODE.get(mode, set())
            | unlocked
        )
        return visible & allowed

    def can_execute(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        session=None,
        mode: str = "coding",
        run_context: Any | None = None,
    ) -> ToolPolicyDecision:
        if tool_name == "tool_search":
            return ToolPolicyDecision(allowed=True)

        tool = self.registry._tools.get(tool_name)
        if tool is None:
            return ToolPolicyDecision(
                allowed=False,
                reason=f"Unknown tool: {tool_name}",
            )
        if not tool.enabled_for(mode, session):
            return ToolPolicyDecision(
                allowed=False,
                reason=f"Tool '{tool_name}' is not allowed in {mode} mode.",
            )
        if session is not None and tool_name not in self.visible_tools(
            session,
            mode,
            run_context=run_context,
        ):
            return ToolPolicyDecision(
                allowed=False,
                reason=(
                    f"Tool '{tool_name}' is not visible in this turn. "
                    f"Call tool_search with query='select:{tool_name}' first."
                ),
                requires_approval=self.requires_approval(
                    tool_name,
                    args or {},
                    session=session,
                    mode=mode,
                    run_context=run_context,
                ),
            )
        return ToolPolicyDecision(allowed=True)

    def requires_approval(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        session=None,
        mode: str = "coding",
        run_context: Any | None = None,
    ) -> bool:
        return False

    def _allowed_names(self, *, session=None, mode: str = "coding") -> set[str]:
        return {
            name
            for name, tool in self.registry._tools.items()
            if tool.enabled_for(mode, session)
        }
