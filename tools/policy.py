from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tools.spec import ToolExposure

UNLOCKED_TOOLS_KEY = "unlocked_tools"

@dataclass(frozen=True)
class ToolPolicyDecision:
    allowed: bool
    reason: str = ""


class ToolPolicy:
    """Runtime policy for tool visibility and execution decisions."""

    def __init__(self, registry) -> None:
        self.registry = registry

    def visible_tools(
        self,
        session,
        mode: str = "coding",
        run_context: Any | None = None,
        agent_spec: Any | None = None,
    ) -> set[str]:
        allowed = self._allowed_names(
            session=session,
            mode=mode,
            agent_spec=agent_spec,
            run_context=run_context,
        )
        metadata = getattr(session, "metadata", {}) or {}
        unlocked = set(metadata.get(UNLOCKED_TOOLS_KEY, []))
        visible = {
            name
            for name, tool in self.registry._tools.items()
            if tool.exposure is ToolExposure.PRELOADED
            or (
                tool.exposure is ToolExposure.CONDITIONAL
                and self._condition_met(tool.condition, session=session, run_context=run_context)
            )
        } | unlocked
        return visible & allowed

    def can_execute(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        session=None,
        mode: str = "coding",
        run_context: Any | None = None,
        agent_spec: Any | None = None,
    ) -> ToolPolicyDecision:
        tool = self.registry._tools.get(tool_name)
        if tool is None:
            return ToolPolicyDecision(
                allowed=False,
                reason=f"Unknown tool: {tool_name}",
            )
        agent_type = self._agent_type(agent_spec)
        if not tool.enabled_for(mode, session, agent_type=agent_type):
            return ToolPolicyDecision(
                allowed=False,
                reason=f"Tool '{tool_name}' is not allowed in {mode} mode.",
            )
        if tool_name == "load_skill" and not self._skills_enabled(agent_spec):
            return ToolPolicyDecision(
                allowed=False,
                reason="Skill capabilities are disabled by ContextPolicy.include_skills.",
            )
        spawn_error = self._spawn_policy_error(
            tool_name,
            args or {},
            agent_spec=agent_spec,
        )
        if spawn_error:
            return ToolPolicyDecision(allowed=False, reason=spawn_error)
        if session is not None and tool_name not in self.visible_tools(
            session,
            mode,
            run_context=run_context,
            agent_spec=agent_spec,
        ):
            return ToolPolicyDecision(
                allowed=False,
                reason=(
                    f"Tool '{tool_name}' is not visible in this turn. "
                    "Discover it with tool_search first."
                ),
            )
        return ToolPolicyDecision(allowed=True)

    def _allowed_names(
        self,
        *,
        session=None,
        mode: str = "coding",
        agent_spec: Any | None = None,
        run_context: Any | None = None,
    ) -> set[str]:
        agent_type = self._agent_type(agent_spec)
        names = {
            name
            for name, tool in self.registry._tools.items()
            if tool.exposure is not ToolExposure.INTERNAL
            and tool.enabled_for(mode, session, agent_type=agent_type)
        }
        if not self._skills_enabled(agent_spec):
            names.discard("load_skill")
        spawn_policy = getattr(agent_spec, "spawn_policy", None)
        if agent_spec is not None and not bool(getattr(spawn_policy, "enabled", False)):
            names -= {"task", "parallel_tasks"}
        tool_set = getattr(agent_spec, "tool_set", None)
        allow = set(getattr(tool_set, "allow", ()) or ())
        deny = set(getattr(tool_set, "deny", ()) or ())
        if allow:
            names &= allow
        return names - deny

    @staticmethod
    def _skills_enabled(agent_spec: Any | None) -> bool:
        context_policy = getattr(agent_spec, "context_policy", None)
        return bool(getattr(context_policy, "include_skills", True))

    @staticmethod
    def _spawn_policy_error(
        tool_name: str,
        args: dict[str, Any],
        *,
        agent_spec: Any | None,
    ) -> str:
        if tool_name not in {"task", "parallel_tasks"}:
            return ""
        policy = getattr(agent_spec, "spawn_policy", None)
        if not bool(getattr(policy, "enabled", False)):
            return "Subagent spawning is disabled by AgentSpec.spawn_policy."
        allowed = set(getattr(policy, "allowed_agent_types", ()) or ())
        requested: list[str] = []
        if tool_name == "task":
            requested.append(str(args.get("agent_type") or "explore"))
        else:
            for item in args.get("tasks") or []:
                if isinstance(item, dict):
                    requested.append(str(item.get("agent_type") or "explore"))
        denied = sorted({value for value in requested if value not in allowed})
        if denied:
            return (
                "AgentSpec.spawn_policy does not allow agent type(s): "
                + ", ".join(denied)
            )
        return ""

    @staticmethod
    def _agent_type(agent_spec: Any | None) -> str:
        metadata = getattr(agent_spec, "metadata", {}) or {}
        return str(metadata.get("agent_type") or getattr(agent_spec, "name", "") or "")

    @staticmethod
    def _condition_met(condition: str, *, session=None, run_context=None) -> bool:
        if condition == "task_state_active":
            metadata = getattr(session, "metadata", {}) or {}
            return isinstance(metadata.get("task_state"), dict)
        return False
