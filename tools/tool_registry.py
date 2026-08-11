import json
from typing import Callable, Any

from tools.policy import UNLOCKED_TOOLS_KEY, ToolPolicy
from tools.spec import ToolInjection, ToolRisk, ToolSpec, ToolStateEffect
_SESSION_SCOPED_TOOLS = {
    "update_task_state",
    "bash",
    "list_files",
    "rg",
    "grep",
    "nl",
    "repo_map",
    "code_outline",
    "read_file",
    "read_files",
    "read_artifact",
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


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self.policy = ToolPolicy(self)

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def spec_for(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def catalog(self, *, mode: str | None = None) -> list[dict[str, Any]]:
        items = []
        for tool in self._tools.values():
            if mode is not None and not tool.enabled_for(mode):
                continue
            items.append({
                "name": tool.name,
                "description": tool.description,
                "risk": tool.risk.value,
                "source": tool.source,
                "allowed_modes": sorted(tool.allowed_modes),
                "injection": tool.injection.value,
                "idempotent": tool.idempotent,
                "side_effect": tool.side_effect,
            })
        return sorted(items, key=lambda item: item["name"])

    def governance_catalog(self, *, mode: str | None = None) -> list[dict[str, Any]]:
        """Return internal governance metadata without changing the model catalog."""
        items = []
        for tool in self._tools.values():
            if mode is not None and not tool.enabled_for(mode):
                continue
            items.append({"name": tool.name, **tool.governance_dict()})
        return sorted(items, key=lambda item: item["name"])

    def schemas_for_mode(self, mode: str = "coding") -> list[dict]:
        return [
            self._schema_for_mode(tool, mode)
            for tool in self._tools.values()
            if tool.enabled_for(mode)
        ]

    def schemas_for_turn(self, session, mode: str = "coding") -> list[dict]:
        visible_names = self.visible_names_for_turn(session, mode)
        return [
            self._schema_for_mode(tool, mode)
            for name, tool in self._tools.items()
            if name in visible_names
        ]

    def _schema_for_mode(self, tool: ToolSpec, mode: str) -> dict:
        return tool.schema_for(mode)

    def visible_names_for_turn(self, session, mode: str = "coding") -> set[str]:
        return self.policy.visible_tools(session, mode)

    def tool_catalog_text(self, session, mode: str = "coding") -> str:
        allowed = self.policy._allowed_names(session=session, mode=mode)
        visible = self.visible_names_for_turn(session, mode) if session is not None else set()
        deferred = sorted(name for name in allowed if name not in visible)

        if not deferred:
            return ""

        lines = [
            '<tool_catalog>',
            "Deferred tools can be unlocked with tool_search select:<name>:",
            ", ".join(deferred),
        ]
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
    ) -> Any:
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
            if (
                trace_store is not None
                and run_state is not None
                and tool.requires_audit
            ):
                try:
                    trace_store.append_event(
                        run_state,
                        "tool.governance.observed",
                        {
                            "tool_name": name,
                            **tool.governance_dict(),
                        },
                        parent_span_id=parent_span_id,
                    )
                except Exception:
                    pass
            handler_args = dict(args)
            if tool.session_scoped:
                handler_args["_session"] = session
            if "_mode" in tool.runtime_parameters:
                handler_args["_mode"] = mode
            if "_trace_store" in tool.runtime_parameters:
                handler_args["_trace_store"] = trace_store
            if "_run_state" in tool.runtime_parameters:
                handler_args["_run_state"] = run_state
            if "_parent_span_id" in tool.runtime_parameters:
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
        function = self._schema_for_mode(self._tools[name], mode).get("function", {})
        parameters = function.get("parameters", {})
        return "\n".join([
            f"Tool: {name}",
            f"Description: {function.get('description', '')}",
            "Parameters:",
            json.dumps(parameters, indent=2, ensure_ascii=False),
        ])

    def _tool_description(self, name: str, *, mode: str = "coding") -> str:
        tool = self._tools.get(name)
        if tool is None:
            return ""
        return self._schema_for_mode(tool, mode)["function"].get("description", "")


from .schema import CORE_TASK_STATE_TOOL, LEAD_TOOLS, SEARCH_TOOLS, TEAMMATE_TOOLS
from .handlers import make_lead_handlers, make_teammate_handlers


def build_lead_tool_registry(team=None, *, artifact_store=None) -> ToolRegistry:
    registry = ToolRegistry()
    if team is None:
        from applications.coding.orchestration.teammate import TEAM

        team = TEAM
    handlers = make_lead_handlers(team, artifact_store=artifact_store)

    for schema in LEAD_TOOLS:
        name = schema["function"]["name"]
        handler = handlers.get(name)
        if handler is None and name != "tool_search":
            continue

        registry.register(_builtin_spec(
            schema,
            handler or (lambda **kw: "tool_search is handled by ToolRegistry."),
            source="lead",
        ))

    return registry


def build_teammate_tool_registry(name: str, *, artifact_store=None) -> ToolRegistry:
    registry = ToolRegistry()
    handlers = make_teammate_handlers(name, artifact_store=artifact_store)

    for schema in TEAMMATE_TOOLS + SEARCH_TOOLS:
        tool_name = schema["function"]["name"]
        handler = handlers.get(tool_name)
        if handler is None and tool_name != "tool_search":
            continue
        registry.register(_builtin_spec(
            schema,
            handler or (lambda **kw: "tool_search is handled by ToolRegistry."),
            source=f"teammate:{name}",
        ))

    return registry


def _risk_for_tool(name: str) -> ToolRisk:
    if name in {
        "bash",
        "write_file",
        "edit_file",
        "background_run",
        "git_add",
        "git_commit",
    }:
        return ToolRisk.HIGH
    if name in {
        "list_files",
        "rg",
        "grep",
        "nl",
        "repo_map",
        "code_outline",
        "read_file",
        "read_files",
        "read_artifact",
        "retrieve_tool_result",
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
        return ToolRisk.LOW
    if name == "tool_search":
        return ToolRisk.LOW
    return ToolRisk.NORMAL


_NON_IDEMPOTENT_TOOLS = {
    "bash", "write_file", "edit_file", "storage_write_file",
    "sandbox_write_file", "publish_artifact", "git_add", "git_commit",
    "memorize", "update_task_state", "task_create", "task_update",
    "claim_task", "background_run", "task", "parallel_tasks",
    "spawn_teammate", "broadcast", "send_message", "shutdown_request",
    "shutdown_response", "plan_approval", "plan_approval_request",
}

_ALWAYS_TOOLS = {"tool_search"}
_DEFERRED_TOOLS = {
    "bash", "write_file", "edit_file", "background_run", "git_add",
    "git_commit", "spawn_teammate", "list_teammates", "broadcast",
    "shutdown_request", "shutdown_status", "plan_approval", "claim_task",
    "load_skill", "update_task_state", "memorize", "recall_memory",
    "read_artifact", "retrieve_tool_result", "storage_list_files",
    "storage_read_file", "storage_write_file", "sandbox_list_files",
    "sandbox_read_file", "sandbox_write_file", "publish_artifact",
}


def _builtin_spec(schema: dict, handler: Callable[..., str], *, source: str) -> ToolSpec:
    name = str(schema["function"]["name"])
    non_idempotent = name in _NON_IDEMPOTENT_TOOLS
    if name in _ALWAYS_TOOLS:
        injection = ToolInjection.ALWAYS
    elif name in _DEFERRED_TOOLS:
        injection = ToolInjection.DEFERRED
    else:
        injection = ToolInjection.PRELOADED
    state_effect = ToolStateEffect.NONE
    policy_tag = ""
    if name == "memorize":
        state_effect = ToolStateEffect.AGENT_STATE
        policy_tag = "memory.write"
    elif name == "recall_memory":
        policy_tag = "memory.read"
    elif non_idempotent:
        state_effect = ToolStateEffect.EXTERNAL
    return ToolSpec(
        schema=schema,
        handler=handler,
        allowed_modes=frozenset(_modes_for_tool(name)),
        risk=_risk_for_tool(name),
        idempotent=not non_idempotent,
        side_effect=non_idempotent,
        state_effect=state_effect,
        injection=injection,
        source=source,
        session_scoped=name in _SESSION_SCOPED_TOOLS,
        policy_tag=policy_tag,
        runtime_parameters=frozenset(
            ({"_mode"} if name == "update_task_state" else set())
            | (
                {"_trace_store", "_run_state", "_parent_span_id"}
                if name in {"task", "parallel_tasks"}
                else set()
            )
        ),
        schemas_by_mode=(
            {"bot": CORE_TASK_STATE_TOOL, "hybrid": CORE_TASK_STATE_TOOL}
            if name == "update_task_state"
            else {}
        ),
    )


def _modes_for_tool(name: str) -> set[str]:
    coding_tools = {
        "update_task_state",
        "bash",
        "list_files",
        "rg",
        "grep",
        "nl",
        "repo_map",
        "code_outline",
        "read_file",
        "read_files",
        "read_artifact",
        "retrieve_tool_result",
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
        "update_task_state",
        "bash",
        "list_files",
        "rg",
        "grep",
        "nl",
        "repo_map",
        "code_outline",
        "read_file",
        "read_files",
        "retrieve_tool_result",
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
        "update_task_state",
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
    if name in {"memorize", "recall_memory", "tool_search", "retrieve_tool_result"}:
        enabled.update({"bot", "coding", "teammate"})
    if name == "read_artifact":
        enabled.update({"bot", "coding", "teammate"})
    if name == "update_task_state":
        enabled.update({"bot", "hybrid"})
    return enabled
