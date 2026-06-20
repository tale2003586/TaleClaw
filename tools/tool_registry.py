from dataclasses import dataclass
import json
from typing import Callable, Any

from tools.policy import (
    ALWAYS_ON_TOOLS,
    DEFERRED_TOOLS,
    PRELOADED_TOOLS_BY_MODE,
    UNLOCKED_TOOLS_KEY,
    ToolPolicy,
)
SESSION_SCOPED_TOOLS = {
    "bash",
    "list_files",
    "rg",
    "grep",
    "nl",
    "repo_map",
    "code_outline",
    "read_file",
    "write_file",
    "edit_file",
    "git_status",
    "git_diff",
    "git_log",
    "git_branch",
    "git_add",
    "git_commit",
    "memorize",
    "recall_memory",
    "storage_list_files",
    "storage_read_file",
    "storage_write_file",
    "sandbox_list_files",
    "sandbox_read_file",
    "sandbox_write_file",
    "publish_artifact",
    "task",
    "parallel_tasks",
}


@dataclass
class ToolSpec:
    name: str
    schema: dict
    handler: Callable[..., str]
    risk: str = "normal"
    enabled_modes: set[str] | None = None
    source: str = "local"
    always_on: bool = False
    session_scoped: bool = False
    admin_only: bool = False

    def enabled_for(self, mode: str, session=None) -> bool:
        if self.enabled_modes is not None and mode not in self.enabled_modes:
            return False
        if self.admin_only and session is not None:
            metadata = getattr(session, "metadata", {}) or {}
            return metadata.get("user_role", "admin") == "admin"
        return True


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self.policy = ToolPolicy(self)

    def register(
        self,
        schema: dict,
        handler: Callable[..., str],
        *,
        risk: str = "normal",
        enabled_modes: set[str] | None = None,
        source: str = "local",
        always_on: bool = False,
        session_scoped: bool = False,
        admin_only: bool = False,
    ) -> None:
        name = schema["function"]["name"]
        self._tools[name] = ToolSpec(
            name=name,
            schema=schema,
            handler=handler,
            risk=risk,
            enabled_modes=enabled_modes,
            source=source,
            always_on=always_on,
            session_scoped=session_scoped,
            admin_only=admin_only,
        )

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def catalog(self, *, mode: str | None = None) -> list[dict[str, Any]]:
        items = []
        for tool in self._tools.values():
            if mode is not None and not tool.enabled_for(mode):
                continue
            items.append({
                "name": tool.name,
                "description": tool.schema["function"].get("description", ""),
                "risk": tool.risk,
                "source": tool.source,
                "enabled_modes": (
                    sorted(tool.enabled_modes)
                    if tool.enabled_modes is not None
                    else None
                ),
                "always_on": tool.always_on,
            })
        return sorted(items, key=lambda item: item["name"])

    def schemas_for_mode(self, mode: str = "coding") -> list[dict]:
        return [
            tool.schema
            for tool in self._tools.values()
            if tool.enabled_for(mode)
        ]

    def schemas_for_turn(self, session, mode: str = "coding") -> list[dict]:
        visible_names = self.visible_names_for_turn(session, mode)
        return [
            tool.schema
            for name, tool in self._tools.items()
            if name in visible_names
        ]

    def visible_names_for_turn(self, session, mode: str = "coding") -> set[str]:
        return self.policy.visible_tools(session, mode)

    def tool_catalog_text(self, session, mode: str = "coding") -> str:
        allowed = self.policy._allowed_names(session=session, mode=mode)
        visible = self.visible_names_for_turn(session, mode) if session is not None else set()
        direct = sorted(name for name in visible if name in allowed)
        deferred = sorted(name for name in allowed if name not in visible)

        if not direct and not deferred:
            return ""

        lines = [
            '<tool_catalog>',
            "Tools are workspace-scoped when they operate on files. Use relative paths.",
            "Call visible tools directly. Use tool_search with help:<tool_name> for parameters.",
        ]
        if direct:
            lines.append("Visible now:")
            for name in direct:
                lines.append(f"- {name}: {self._tool_description(name)}")
        if deferred:
            lines.append("Available after unlock:")
            for name in deferred:
                lines.append(
                    f"- {name}: {self._tool_description(name)} "
                    f"(unlock with tool_search select:{name})"
                )
        lines.append("</tool_catalog>")
        return "\n".join(lines)

    def reset_turn_unlocks(self, session) -> None:
        session.metadata[UNLOCKED_TOOLS_KEY] = []

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        session=None,
        mode: str = "coding",
        trace_store=None,
        run_state=None,
        parent_span_id: str | None = None,
    ) -> str:
        if name == "tool_search":
            return self._tool_search(args.get("query", ""), session=session, mode=mode)

        availability_error = self.execution_error_for_turn(
            name,
            session=session,
            mode=mode,
        )
        if availability_error:
            return availability_error

        tool = self._tools[name]
        try:
            handler_args = dict(args)
            if tool.session_scoped or name in SESSION_SCOPED_TOOLS:
                handler_args["_session"] = session
            if name in {"task", "parallel_tasks"}:
                handler_args["_trace_store"] = trace_store
                handler_args["_run_state"] = run_state
                handler_args["_parent_span_id"] = parent_span_id
            return tool.handler(**handler_args)
        except Exception as e:
            return f"Error: {e}"

    def execution_error_for_turn(
        self,
        name: str,
        *,
        session=None,
        mode: str = "coding",
    ) -> str | None:
        if name == "tool_search":
            return None
        decision = self.policy.can_execute(name, session=session, mode=mode)
        return None if decision.allowed else decision.reason

    def _tool_search(self, query: str, *, session=None, mode: str = "coding") -> str:
        query = (query or "").strip()
        allowed = self.policy._allowed_names(session=session, mode=mode)
        lowered_query = query.lower()

        if lowered_query in {"catalog", "tools", "list"}:
            return self.tool_catalog_text(session, mode) or "No tools are available in this mode."

        if lowered_query.startswith(("help:", "schema:")):
            name = query.split(":", 1)[1].strip()
            return self._tool_help(name, allowed=allowed, mode=mode)

        if lowered_query.startswith("select:"):
            name = query.split(":", 1)[1].strip()
            if name not in self._tools:
                return f"Unknown tool: {name}"
            if name not in allowed:
                return f"Tool '{name}' is not allowed in {mode} mode."
            if session is None:
                return "Cannot unlock tool without a session."
            unlocked = list(session.metadata.get(UNLOCKED_TOOLS_KEY, []))
            if name not in unlocked:
                unlocked.append(name)
            session.metadata[UNLOCKED_TOOLS_KEY] = unlocked
            return (
                f"Unlocked tool for this turn: {name}. "
                "You may call it in the next reasoning step."
            )

        visible = self.visible_names_for_turn(session, mode) if session is not None else set()
        matches = []
        for name, tool in self._tools.items():
            if name not in allowed:
                continue
            if name in visible:
                continue
            description = tool.schema["function"].get("description", "")
            haystack = f"{name} {description}".lower()
            if not lowered_query or lowered_query in haystack:
                matches.append((name, description))

        if not matches:
            return "No matching deferred tools are available in this mode."

        lines = ["Deferred tools available. Unlock one with select:<tool_name>:"]
        for name, description in matches[:12]:
            lines.append(f"- {name}: {description}")
        return "\n".join(lines)

    def _tool_help(self, name: str, *, allowed: set[str], mode: str) -> str:
        if name not in self._tools:
            return f"Unknown tool: {name}"
        if name not in allowed:
            return f"Tool '{name}' is not allowed in {mode} mode."
        function = self._tools[name].schema.get("function", {})
        parameters = function.get("parameters", {})
        return "\n".join([
            f"Tool: {name}",
            f"Description: {function.get('description', '')}",
            "Parameters:",
            json.dumps(parameters, indent=2, ensure_ascii=False),
        ])

    def _tool_description(self, name: str) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return ""
        return tool.schema["function"].get("description", "")


from .schema import LEAD_TOOLS, SEARCH_TOOLS, TEAMMATE_TOOLS
from .handlers import make_lead_handlers, make_teammate_handlers


def build_lead_tool_registry(team=None) -> ToolRegistry:
    registry = ToolRegistry()
    if team is None:
        from coding_runtime.teammate import TEAM

        team = TEAM
    handlers = make_lead_handlers(team)

    for schema in LEAD_TOOLS:
        name = schema["function"]["name"]
        handler = handlers.get(name)
        if handler is None and name != "tool_search":
            continue

        registry.register(
            schema,
            handler or (lambda **kw: "tool_search is handled by ToolRegistry."),
            risk=_risk_for_tool(name),
            enabled_modes=_modes_for_tool(name),
            source="lead",
        )

    return registry


def build_teammate_tool_registry(name: str) -> ToolRegistry:
    registry = ToolRegistry()
    handlers = make_teammate_handlers(name)

    for schema in TEAMMATE_TOOLS + SEARCH_TOOLS:
        tool_name = schema["function"]["name"]
        handler = handlers.get(tool_name)
        if handler is None and tool_name != "tool_search":
            continue
        registry.register(
            schema,
            handler or (lambda **kw: "tool_search is handled by ToolRegistry."),
            risk=_risk_for_tool(tool_name),
            enabled_modes=_modes_for_tool(tool_name),
            source=f"teammate:{name}",
        )

    return registry


def _risk_for_tool(name: str) -> str:
    if name in {
        "bash",
        "write_file",
        "edit_file",
        "background_run",
        "git_add",
        "git_commit",
    }:
        return "high"
    if name in {
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
        "storage_list_files",
        "storage_read_file",
        "sandbox_list_files",
        "sandbox_read_file",
        "task_list",
        "task_get",
        "check_background",
    }:
        return "low"
    if name == "tool_search":
        return "low"
    return "normal"


def _modes_for_tool(name: str) -> set[str]:
    coding_tools = {
        "bash",
        "list_files",
        "rg",
        "grep",
        "nl",
        "repo_map",
        "code_outline",
        "read_file",
        "write_file",
        "edit_file",
        "git_status",
        "git_diff",
        "git_log",
        "git_branch",
        "git_add",
        "git_commit",
        "load_skill",
        "task_create",
        "task_update",
        "task_list",
        "task_get",
        "claim_task",
        "background_run",
        "check_background",
        "compact",
        "task",
        "parallel_tasks",
        "spawn_teammate",
        "list_teammates",
        "broadcast",
        "send_message",
        "read_inbox",
        "shutdown_request",
        "shutdown_status",
        "plan_approval",
    }

    teammate_tools = {
        "bash",
        "list_files",
        "rg",
        "grep",
        "nl",
        "repo_map",
        "code_outline",
        "read_file",
        "write_file",
        "edit_file",
        "git_status",
        "git_diff",
        "git_log",
        "git_branch",
        "git_add",
        "git_commit",
        "load_skill",
        "task_create",
        "task_update",
        "task_list",
        "task_get",
        "claim_task",
        "background_run",
        "check_background",
        "send_message",
        "read_inbox",
        "idle",
        "shutdown_response",
        "plan_approval_request",
    }

    bot_tools = {
        "load_skill",
        "storage_list_files",
        "storage_read_file",
        "storage_write_file",
        "sandbox_list_files",
        "sandbox_read_file",
        "sandbox_write_file",
        "publish_artifact",
    }

    enabled = set()
    if name in coding_tools:
        enabled.add("coding")
    if name in teammate_tools:
        enabled.add("teammate")
    if name in bot_tools:
        enabled.add("bot")
    if name in {
        "storage_list_files",
        "storage_read_file",
        "storage_write_file",
        "sandbox_list_files",
        "sandbox_read_file",
        "sandbox_write_file",
        "publish_artifact",
    }:
        enabled.add("coding")
    if name in {"memorize", "recall_memory", "tool_search"}:
        enabled.update({"bot", "coding", "teammate"})
    return enabled
