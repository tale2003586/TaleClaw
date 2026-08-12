from __future__ import annotations

from dataclasses import dataclass
from typing import Any

UNLOCKED_TOOLS_KEY = "unlocked_tools"

DEFAULT_VISIBLE_TOOLS = {
    "bot": frozenset({"tool_search"}),
    "hybrid": frozenset({"tool_search"}),
    "coding": frozenset({
        "bash",
        "list_files",
        "rg",
        "read_file",
        "write_file",
        "edit_file",
        "update_task_state",
        "tool_search",
    }),
    "teammate": frozenset({
        "bash",
        "idle",
        "list_files",
        "rg",
        "read_file",
        "send_message",
        "write_file",
        "edit_file",
        "update_task_state",
        "tool_search",
    }),
}


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
        visible = set(DEFAULT_VISIBLE_TOOLS.get(mode, ())) | unlocked
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
