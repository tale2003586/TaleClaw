import json
import re
from dataclasses import dataclass, field
from typing import Callable, Any

from tools.policy import UNLOCKED_TOOLS_KEY, ToolPolicy
from tools.spec import ToolExposure, ToolRisk, ToolSpec, ToolStateEffect
from tools.schema import CORE_TASK_STATE_TOOL, LEAD_TOOLS, SEARCH_TOOLS, TEAMMATE_TOOLS
from tools.handlers import (
    make_lead_handlers,
    make_subagent_handlers,
    make_teammate_handlers,
)


_CODING_TEAMMATE = frozenset({"coding", "teammate"})
_ALL_AGENT_MODES = frozenset({"bot", "coding", "teammate"})


@dataclass(frozen=True)
class BuiltinToolDeclaration:
    """Canonical semantic declaration for one built-in tool.

    Schemas remain in ``tools.schema`` and handlers remain in
    ``tools.handlers``. This object owns the runtime semantics used to create
    the ToolSpec which the registry, policy, discovery, and executor consume.
    """

    name: str
    allowed_modes: frozenset[str] = _CODING_TEAMMATE
    risk: ToolRisk = ToolRisk.NORMAL
    idempotent: bool = True
    side_effect: bool = False
    state_effect: ToolStateEffect = ToolStateEffect.NONE
    exposure: ToolExposure = ToolExposure.DEFERRED
    discovery_summary: str = ""
    capabilities: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    allowed_agent_types: frozenset[str] = field(default_factory=frozenset)
    condition: str = ""
    session_scoped: bool = False
    admin_only: bool = False
    policy_tag: str = ""
    runtime_parameters: frozenset[str] = field(default_factory=frozenset)
    schemas_by_mode: dict[str, dict] = field(default_factory=dict)
    teammate_exposure: ToolExposure | None = None
    registry_owned_execution: bool = False

    def bind(self, schema: dict, handler: Callable[..., str], *, source: str) -> ToolSpec:
        schema_name = str(schema.get("function", {}).get("name") or "")
        if schema_name != self.name:
            raise ValueError(
                f"Builtin declaration {self.name!r} does not match schema {schema_name!r}."
            )
        exposure = self.exposure
        if self.teammate_exposure is not None and source.startswith("teammate:"):
            exposure = self.teammate_exposure
        fallback_term = self.name.replace("_", " ")
        return ToolSpec(
            schema=schema,
            handler=handler,
            allowed_modes=self.allowed_modes,
            risk=self.risk,
            idempotent=self.idempotent,
            side_effect=self.side_effect,
            state_effect=self.state_effect,
            exposure=exposure,
            discovery_summary=self.discovery_summary,
            capabilities=self.capabilities or (fallback_term,),
            aliases=self.aliases or (self.name,),
            keywords=self.keywords or (fallback_term,),
            allowed_agent_types=self.allowed_agent_types,
            condition=self.condition,
            source=source,
            session_scoped=self.session_scoped,
            admin_only=self.admin_only,
            policy_tag=self.policy_tag,
            runtime_parameters=self.runtime_parameters,
            schemas_by_mode=self.schemas_by_mode,
        )


def _builtin(name: str, **kwargs) -> BuiltinToolDeclaration:
    return BuiltinToolDeclaration(name=name, **kwargs)


_HIGH_RISK = ToolRisk.HIGH
_LOW_RISK = ToolRisk.LOW
_PRELOADED = ToolExposure.PRELOADED
_CONDITIONAL = ToolExposure.CONDITIONAL
_EXTERNAL_EFFECT = ToolStateEffect.EXTERNAL


BUILTIN_TOOL_DECLARATIONS = (
    _builtin("bash", risk=_HIGH_RISK, idempotent=False, side_effect=True,
             state_effect=_EXTERNAL_EFFECT, exposure=_PRELOADED, session_scoped=True),
    _builtin("list_files", risk=_LOW_RISK, exposure=_PRELOADED, session_scoped=True),
    _builtin("rg", risk=_LOW_RISK, exposure=_PRELOADED, session_scoped=True),
    _builtin("grep", risk=_LOW_RISK, session_scoped=True),
    _builtin("nl", risk=_LOW_RISK, session_scoped=True),
    _builtin("repo_map", risk=_LOW_RISK, session_scoped=True),
    _builtin("code_outline", risk=_LOW_RISK, exposure=_PRELOADED, session_scoped=True),
    _builtin("read_file", risk=_LOW_RISK, exposure=_PRELOADED, session_scoped=True),
    _builtin("read_files", risk=_LOW_RISK, exposure=_PRELOADED, session_scoped=True),
    _builtin("read_artifact", allowed_modes=_ALL_AGENT_MODES, risk=_LOW_RISK,
             session_scoped=True),
    _builtin("retrieve_tool_result", allowed_modes=_ALL_AGENT_MODES, risk=_LOW_RISK,
             discovery_summary="Retrieve a previously externalized large tool result.",
             capabilities=("tool result retrieval", "large result access"),
             aliases=("retrieve result", "读取工具结果", "取回大结果"),
             keywords=("artifact", "tool output", "result reference")),
    _builtin("write_file", risk=_HIGH_RISK, idempotent=False, side_effect=True,
             state_effect=_EXTERNAL_EFFECT, exposure=_PRELOADED, session_scoped=True),
    _builtin("edit_file", risk=_HIGH_RISK, idempotent=False, side_effect=True,
             state_effect=_EXTERNAL_EFFECT, exposure=_PRELOADED, session_scoped=True),
    _builtin("git_status", risk=_LOW_RISK, session_scoped=True),
    _builtin("git_diff", risk=_LOW_RISK, session_scoped=True),
    _builtin("git_log", risk=_LOW_RISK, session_scoped=True),
    _builtin("git_branch", risk=_LOW_RISK, session_scoped=True),
    _builtin("git_add", risk=_HIGH_RISK, idempotent=False, side_effect=True,
             state_effect=_EXTERNAL_EFFECT, session_scoped=True,
             discovery_summary="Stage selected repository changes for a Git commit.",
             capabilities=("git stage", "version control write"),
             aliases=("stage changes", "stage these changes", "git add", "暂存修改"),
             keywords=("git", "index", "commit preparation")),
    _builtin("git_commit", risk=_HIGH_RISK, idempotent=False, side_effect=True,
             state_effect=_EXTERNAL_EFFECT, session_scoped=True,
             discovery_summary="Create a Git commit from staged changes.",
             capabilities=("git commit", "version control write"),
             aliases=("commit changes", "提交代码", "创建提交"),
             keywords=("git", "commit", "版本控制")),
    _builtin("load_skill", allowed_modes=_ALL_AGENT_MODES,
             discovery_summary="Discover and load a specialized workflow from an installed skill.",
             capabilities=("skill discovery", "specialized workflow", "procedure"),
             aliases=("skill", "load workflow", "技能", "流程", "操作指南"),
             keywords=("specialized instruction", "适合这个任务", "专业流程")),
    _builtin("update_task_state", allowed_modes=frozenset({"bot", "coding", "hybrid", "teammate"}),
             idempotent=False, side_effect=True, state_effect=_EXTERNAL_EFFECT,
             exposure=_CONDITIONAL, condition="task_state_active", session_scoped=True,
             runtime_parameters=frozenset({"_mode"}),
             schemas_by_mode={"bot": CORE_TASK_STATE_TOOL, "hybrid": CORE_TASK_STATE_TOOL}),
    _builtin("task_create", idempotent=False, side_effect=True, state_effect=_EXTERNAL_EFFECT),
    _builtin("task_update", idempotent=False, side_effect=True, state_effect=_EXTERNAL_EFFECT),
    _builtin("task_list", risk=_LOW_RISK),
    _builtin("task_get", risk=_LOW_RISK),
    _builtin("claim_task", idempotent=False, side_effect=True, state_effect=_EXTERNAL_EFFECT,
             discovery_summary="Claim an available team task for execution.",
             capabilities=("claim task", "team task coordination"),
             aliases=("take task", "领取任务", "认领任务"), keywords=("task", "team", "claim")),
    _builtin("background_run", risk=_HIGH_RISK, idempotent=False, side_effect=True,
             state_effect=_EXTERNAL_EFFECT,
             discovery_summary="Start a long-running command as a background task.",
             capabilities=("background task", "long running command"),
             aliases=("run in background", "后台运行", "后台任务"),
             keywords=("async", "long process", "后台")),
    _builtin("check_background", risk=_LOW_RISK),
    _builtin("send_message", idempotent=False, side_effect=True, state_effect=_EXTERNAL_EFFECT,
             teammate_exposure=_PRELOADED),
    _builtin("read_inbox"),
    _builtin("idle", allowed_modes=frozenset({"teammate"}), exposure=_PRELOADED),
    _builtin("shutdown_response", allowed_modes=frozenset({"teammate"}), idempotent=False,
             side_effect=True, state_effect=_EXTERNAL_EFFECT),
    _builtin("plan_approval_request", allowed_modes=frozenset({"teammate"}), idempotent=False,
             side_effect=True, state_effect=_EXTERNAL_EFFECT),
    _builtin("task", allowed_modes=frozenset({"coding"}), idempotent=False, side_effect=True,
             state_effect=_EXTERNAL_EFFECT, session_scoped=True,
             runtime_parameters=frozenset({"_trace_store", "_run_state", "_parent_span_id"}),
             discovery_summary="Delegate one bounded task to a specialized subagent.",
             capabilities=("subagent", "delegate task", "research worker"),
             aliases=("delegate", "spawn subagent", "子智能体", "委派", "拆任务"),
             keywords=("agent", "worker", "parallel research", "子任务")),
    _builtin("parallel_tasks", allowed_modes=frozenset({"coding"}), idempotent=False,
             side_effect=True, state_effect=_EXTERNAL_EFFECT, session_scoped=True,
             runtime_parameters=frozenset({"_trace_store", "_run_state", "_parent_span_id"}),
             discovery_summary="Run multiple independent subagent tasks in parallel.",
             capabilities=("parallel subagents", "parallel delegation", "fan out"),
             aliases=("parallel agents", "并行子智能体", "并行分析", "并行委派"),
             keywords=("subagent", "delegate", "workers", "拆任务")),
    _builtin("compact", allowed_modes=frozenset({"coding"})),
    _builtin("spawn_teammate", allowed_modes=frozenset({"coding"}), idempotent=False,
             side_effect=True, state_effect=_EXTERNAL_EFFECT,
             discovery_summary="Start a persistent coding teammate for collaborative work.",
             capabilities=("teammate", "collaborative agent", "delegate coding"),
             aliases=("spawn teammate", "启动队友", "协作智能体"),
             keywords=("agent", "team", "collaboration", "协作")),
    _builtin("list_teammates", allowed_modes=frozenset({"coding"}),
             discovery_summary="List active collaborative teammates and their status.",
             capabilities=("teammate status", "agent roster"),
             aliases=("list agents", "队友列表", "智能体状态"), keywords=("team", "agents", "status")),
    _builtin("broadcast", allowed_modes=frozenset({"coding"}), idempotent=False,
             side_effect=True, state_effect=_EXTERNAL_EFFECT,
             discovery_summary="Send one message to all active teammates.",
             capabilities=("team broadcast", "agent communication"),
             aliases=("message all agents", "广播消息", "通知所有队友"),
             keywords=("team", "message", "communication")),
    _builtin("shutdown_request", allowed_modes=frozenset({"coding"}), idempotent=False,
             side_effect=True, state_effect=_EXTERNAL_EFFECT,
             discovery_summary="Request a teammate to stop after completing safe cleanup.",
             capabilities=("teammate shutdown", "agent lifecycle"),
             aliases=("stop teammate", "关闭队友", "停止智能体"), keywords=("shutdown", "agent", "team")),
    _builtin("shutdown_status", allowed_modes=frozenset({"coding"}),
             discovery_summary="Inspect teammate shutdown request status.",
             capabilities=("shutdown status", "agent lifecycle status"),
             aliases=("check shutdown", "关闭状态"), keywords=("shutdown", "status", "agent")),
    _builtin("plan_approval", allowed_modes=frozenset({"coding"}), idempotent=False,
             side_effect=True, state_effect=_EXTERNAL_EFFECT,
             discovery_summary="Approve or reject a teammate's submitted plan.",
             capabilities=("plan approval", "teammate governance"),
             aliases=("approve plan", "审批计划", "批准方案"), keywords=("plan", "approval", "team")),
    _builtin("memorize", allowed_modes=_ALL_AGENT_MODES, idempotent=False, side_effect=True,
             state_effect=ToolStateEffect.AGENT_STATE, session_scoped=True, policy_tag="memory.write",
             discovery_summary="Save a durable fact or preference to long-term memory.",
             capabilities=("memory write", "save preference", "remember fact"),
             aliases=("remember", "记住", "保存偏好", "长期保存"),
             keywords=("memory", "preference", "long term", "以后记得")),
    _builtin("recall_memory", allowed_modes=_ALL_AGENT_MODES, session_scoped=True,
             policy_tag="memory.read",
             discovery_summary="Search long-term memory for prior facts and preferences.",
             capabilities=("memory recall", "memory search", "prior context"),
             aliases=("recall", "previous preference", "mentioned before", "回忆", "之前说过", "以前偏好"),
             keywords=("memory", "previous preference", "长期记忆")),
    _builtin("storage_list_files", allowed_modes=frozenset({"bot", "coding"}), risk=_LOW_RISK,
             session_scoped=True),
    _builtin("storage_read_file", allowed_modes=frozenset({"bot", "coding"}), risk=_LOW_RISK,
             session_scoped=True),
    _builtin("storage_write_file", allowed_modes=frozenset({"bot", "coding"}), idempotent=False,
             side_effect=True, state_effect=_EXTERNAL_EFFECT, session_scoped=True),
    _builtin("sandbox_list_files", allowed_modes=frozenset({"bot", "coding"}), risk=_LOW_RISK,
             session_scoped=True),
    _builtin("sandbox_read_file", allowed_modes=frozenset({"bot", "coding"}), risk=_LOW_RISK,
             session_scoped=True),
    _builtin("sandbox_write_file", allowed_modes=frozenset({"bot", "coding"}), idempotent=False,
             side_effect=True, state_effect=_EXTERNAL_EFFECT, session_scoped=True),
    _builtin("publish_artifact", allowed_modes=frozenset({"bot", "coding"}), idempotent=False,
             side_effect=True, state_effect=_EXTERNAL_EFFECT, session_scoped=True),
    _builtin("tool_search", allowed_modes=_ALL_AGENT_MODES, risk=_LOW_RISK, exposure=_PRELOADED,
             registry_owned_execution=True),
)


def index_builtin_declarations(
    declarations: tuple[BuiltinToolDeclaration, ...] | list[BuiltinToolDeclaration],
) -> dict[str, BuiltinToolDeclaration]:
    indexed: dict[str, BuiltinToolDeclaration] = {}
    for declaration in declarations:
        if declaration.name in indexed:
            raise ValueError(f"Duplicate builtin tool declaration: {declaration.name}")
        indexed[declaration.name] = declaration
    return indexed


_BUILTIN_DECLARATIONS_BY_NAME = index_builtin_declarations(BUILTIN_TOOL_DECLARATIONS)


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
                "exposure": tool.exposure.value,
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

    def schemas_for_turn(
        self,
        session,
        mode: str = "coding",
        *,
        agent_spec=None,
        run_context=None,
    ) -> list[dict]:
        visible_names = self.visible_names_for_turn(
            session,
            mode,
            agent_spec=agent_spec,
            run_context=run_context,
        )
        return [
            self._schema_for_mode(tool, mode)
            for name, tool in self._tools.items()
            if name in visible_names
        ]

    def _schema_for_mode(self, tool: ToolSpec, mode: str) -> dict:
        return tool.schema_for(mode)

    def visible_names_for_turn(
        self,
        session,
        mode: str = "coding",
        *,
        agent_spec=None,
        run_context=None,
    ) -> set[str]:
        return self.policy.visible_tools(
            session,
            mode,
            agent_spec=agent_spec,
            run_context=run_context,
        )

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
        agent_spec=None,
        run_context=None,
    ) -> Any:
        availability_error = self.execution_error_for_turn(
            name,
            args=args,
            session=session,
            mode=mode,
            agent_spec=agent_spec,
            run_context=run_context,
        )
        if availability_error:
            return availability_error

        if name == "tool_search":
            return self._tool_search(
                args.get("query", ""),
                session=session,
                mode=mode,
                agent_spec=agent_spec,
                run_context=run_context,
                trace_store=trace_store,
                run_state=run_state,
                parent_span_id=parent_span_id,
            )

        if name == "load_skill":
            return self._load_skill(
                str(args.get("name") or ""),
                mode=mode,
                agent_spec=agent_spec,
            )

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
        args: dict[str, Any] | None = None,
        session=None,
        mode: str = "coding",
        agent_spec=None,
        run_context=None,
    ) -> str | None:
        decision = self.policy.can_execute(
            name,
            args=args,
            session=session,
            mode=mode,
            agent_spec=agent_spec,
            run_context=run_context,
        )
        return None if decision.allowed else decision.reason

    @staticmethod
    def _load_skill(name: str, *, mode: str, agent_spec=None) -> str:
        from skill_runtime import SKILL_LOADER

        scope = tuple(getattr(agent_spec, "skills", ()) or ())
        return SKILL_LOADER.get_content(
            name,
            mode=mode,
            allowed_names=scope or None,
        )

    def _tool_search(
        self,
        query: str,
        *,
        session=None,
        mode: str = "coding",
        agent_spec=None,
        run_context=None,
        trace_store=None,
        run_state=None,
        parent_span_id: str | None = None,
    ) -> str:
        query = (query or "").strip()
        allowed = self.policy._allowed_names(
            session=session,
            mode=mode,
            agent_spec=agent_spec,
            run_context=run_context,
        )
        lowered_query = query.lower()

        if lowered_query in {"catalog", "tools", "list", "能力", "能力列表", "工具"}:
            return "\n".join([
                "Available capability groups:",
                "- web and current information",
                "- memory and preferences",
                "- skills and specialized workflows",
                "- files, code structure, and git",
                "- artifacts, storage, and sandbox",
                "- subagents and background tasks",
            ])

        if lowered_query.startswith(("help:", "schema:")):
            name = query.split(":", 1)[1].strip()
            return self._tool_help(name, allowed=allowed, mode=mode)

        visible = (
            self.visible_names_for_turn(
                session,
                mode,
                agent_spec=agent_spec,
                run_context=run_context,
            )
            if session is not None else set()
        )
        matches: list[tuple[int, str, ToolSpec, str]] = []
        for name, tool in self._tools.items():
            if name not in allowed:
                continue
            if name in visible:
                continue
            if tool.exposure is not ToolExposure.DEFERRED:
                continue
            score, reason = _discovery_score(query, tool)
            if score > 0:
                matches.append((score, name, tool, reason))

        skill_matches = self._skill_matches(
            query,
            allowed=allowed,
            agent_spec=agent_spec,
            mode=mode,
        )
        matches.sort(key=lambda item: (-item[0], item[1]))
        best_score = matches[0][0] if matches else 0
        selected = [
            item for item in matches
            if item[0] >= 25 and item[0] >= best_score - 20
        ][:3]
        unlocked_names = [name for _, name, _, _ in selected]
        if skill_matches and "load_skill" in allowed and "load_skill" not in unlocked_names:
            unlocked_names.append("load_skill")
        if session is not None and unlocked_names:
            unlocked = list(session.metadata.get(UNLOCKED_TOOLS_KEY, []))
            for name in unlocked_names:
                if name not in unlocked:
                    unlocked.append(name)
            session.metadata[UNLOCKED_TOOLS_KEY] = unlocked

        self._trace_discovery(
            trace_store,
            run_state,
            parent_span_id=parent_span_id,
            query=query,
            candidate_count=len(matches),
            matches=[{"name": name, "score": score} for score, name, _, _ in selected],
            skill_matches=skill_matches,
            unlocked=unlocked_names,
            mode=mode,
        )

        if not selected and not skill_matches:
            return (
                "No matching deferred capabilities are available after applying "
                f"mode={mode} and agent policy filters. Try tool_search('catalog')."
            )

        lines = ["Matched capabilities:"]
        for score, name, tool, reason in selected:
            lines.append(
                f"- {name}: {tool.discovery_summary} "
                f"(matched {reason}, score={score}; unlocked for this turn)"
            )
        for match in skill_matches[:3]:
            lines.append(
                f"- skill:{match['name']}: {match['description']} "
                f"(score={match['score']}; load with load_skill)"
            )
        return "\n".join(lines)

    def _skill_matches(self, query: str, *, allowed: set[str], agent_spec, mode: str):
        if "load_skill" not in allowed:
            return []
        context_policy = getattr(agent_spec, "context_policy", None)
        if not bool(getattr(context_policy, "include_skills", True)):
            return []
        from skill_runtime import SKILL_LOADER

        scope = tuple(getattr(agent_spec, "skills", ()) or ())
        return SKILL_LOADER.search(query, mode=mode, allowed_names=scope or None)

    @staticmethod
    def _trace_discovery(
        trace_store,
        run_state,
        *,
        parent_span_id,
        query,
        candidate_count,
        matches,
        skill_matches,
        unlocked,
        mode,
    ) -> None:
        if trace_store is None or run_state is None:
            return
        try:
            trace_store.append_event(
                run_state,
                "capability.discovery",
                {
                    "query": query,
                    "candidate_count": candidate_count,
                    "matched_tools": matches,
                    "matched_skills": [
                        {"name": item["name"], "score": item["score"]}
                        for item in skill_matches[:3]
                    ],
                    "unlocked": unlocked,
                    "no_result_reason": "" if matches or skill_matches else "no_policy_allowed_match",
                    "filters_applied": ["mode", "agent_tool_set", "agent_type", "exposure"],
                    "mode": mode,
                },
                parent_span_id=parent_span_id,
            )
        except Exception:
            pass

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


_SUBAGENT_TOOL_NAMES = frozenset({"task", "parallel_tasks"})


def build_lead_tool_registry(
    team=None,
    *,
    artifact_store=None,
    subagent_runner=None,
    memory_handlers=None,
    include_subagent_tools: bool = True,
) -> ToolRegistry:
    if team is None:
        from applications.coding.orchestration.teammate import TEAM

        team = TEAM
    schemas = LEAD_TOOLS
    handlers = make_lead_handlers(
        team,
        artifact_store=artifact_store,
        subagent_runner=subagent_runner,
        memory_handlers=memory_handlers,
    )
    if not include_subagent_tools:
        schemas = tuple(
            schema
            for schema in schemas
            if schema["function"]["name"] not in _SUBAGENT_TOOL_NAMES
        )
        handlers = {
            name: handler
            for name, handler in handlers.items()
            if name not in _SUBAGENT_TOOL_NAMES
        }
    return build_builtin_registry(
        schemas=schemas,
        handlers=handlers,
        source="lead",
    )


def build_teammate_tool_registry(name: str, *, artifact_store=None) -> ToolRegistry:
    return build_builtin_registry(
        schemas=TEAMMATE_TOOLS + SEARCH_TOOLS,
        handlers=make_teammate_handlers(name, artifact_store=artifact_store),
        source=f"teammate:{name}",
    )


def build_builtin_registry(
    *,
    schemas: tuple[dict, ...] | list[dict],
    handlers: dict[str, Callable[..., str]],
    source: str,
    declarations: dict[str, BuiltinToolDeclaration] | None = None,
) -> ToolRegistry:
    """Bind one declared builtin schema/handler set into a complete registry."""
    declarations = declarations or _BUILTIN_DECLARATIONS_BY_NAME
    registry = ToolRegistry()
    register_builtin_tools(
        registry,
        schemas=schemas,
        handlers=handlers,
        source=source,
        declarations=declarations,
    )
    return registry


def register_builtin_tools(
    registry: ToolRegistry,
    *,
    schemas: tuple[dict, ...] | list[dict],
    handlers: dict[str, Callable[..., str]],
    source: str,
    declarations: dict[str, BuiltinToolDeclaration] | None = None,
) -> None:
    """Register one complete builtin subset into an existing registry."""
    declarations = declarations or _BUILTIN_DECLARATIONS_BY_NAME
    seen_names: set[str] = set()
    declared_names: set[str] = set()

    for schema in schemas:
        name = str(schema.get("function", {}).get("name") or "")
        if not name:
            raise ValueError("Builtin schema is missing function.name.")
        if name in seen_names:
            raise ValueError(f"Duplicate builtin schema declaration: {name}")
        if registry.spec_for(name) is not None:
            raise ValueError(f"Builtin tool is already registered: {name}")
        seen_names.add(name)
        declaration = declarations.get(name)
        if declaration is None:
            raise ValueError(f"Builtin schema has no semantic declaration: {name}")
        declared_names.add(name)
        handler = handlers.get(name)
        if handler is None:
            if not declaration.registry_owned_execution:
                raise ValueError(f"Builtin tool has no handler: {name}")
            handler = _registry_owned_handler
        registry.register(declaration.bind(schema, handler, source=source))

    orphaned_handlers = set(handlers) - declared_names
    if orphaned_handlers:
        names = ", ".join(sorted(orphaned_handlers))
        raise ValueError(f"Builtin handlers have no registered schema: {names}")


def register_lead_subagent_tools(registry: ToolRegistry, subagent_runner) -> None:
    """Complete a lead registry after its Runtime-bound runner is available."""
    if subagent_runner is None:
        raise ValueError(
            "register_lead_subagent_tools requires a constructed subagent runner."
        )
    schemas = tuple(
        schema
        for schema in LEAD_TOOLS
        if schema["function"]["name"] in _SUBAGENT_TOOL_NAMES
    )
    register_builtin_tools(
        registry,
        schemas=schemas,
        handlers=make_subagent_handlers(subagent_runner),
        source="lead",
    )


def _registry_owned_handler(**_kwargs) -> str:
    """Marker for a ToolSpec whose execution is implemented by ToolRegistry."""
    raise RuntimeError("Registry-owned tool execution should not call its ToolSpec handler.")


def _discovery_score(query: str, tool: ToolSpec) -> tuple[int, str]:
    normalized = _normalize_discovery_text(query)
    if not normalized:
        return 0, ""
    fields = (
        (100, "name", (tool.name,)),
        (80, "alias", tool.aliases),
        (60, "capability", tool.capabilities),
        (40, "keyword", tool.keywords),
        (20, "summary", (tool.discovery_summary,)),
    )
    query_tokens = set(_discovery_tokens(normalized))
    best = (0, "")
    for weight, reason, values in fields:
        for value in values:
            candidate = _normalize_discovery_text(value)
            if not candidate:
                continue
            if normalized == candidate:
                score = weight + 20
            elif candidate in normalized or normalized in candidate:
                score = weight
            else:
                overlap = query_tokens & set(_discovery_tokens(candidate))
                score = min(weight, len(overlap) * max(8, weight // 3)) if overlap else 0
            if score > best[0]:
                best = (score, reason)
    if tool.name == "memorize" and _has_recall_intent(normalized):
        return 0, ""
    if tool.name == "recall_memory" and _has_memory_write_intent(normalized):
        return 0, ""
    return best


def _normalize_discovery_text(value: str) -> str:
    return " ".join(str(value or "").lower().replace("_", " ").split())


def _discovery_tokens(value: str) -> list[str]:
    latin = re.findall(r"[a-z0-9]+", value)
    latin += [word[:-1] for word in latin if len(word) > 3 and word.endswith("s")]
    latin += [word[:-3] + "y" for word in latin if len(word) > 4 and word.endswith("ies")]
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", value)
    cjk_parts = [part for text in cjk for part in (text, *[text[i:i + 2] for i in range(len(text) - 1)])]
    return latin + cjk_parts


def _has_recall_intent(value: str) -> bool:
    return any(term in value for term in (
        "recall", "search", "previous", "prior", "before", "之前", "以前",
        "说过", "回忆", "查找", "搜索", "semantic memory",
    ))


def _has_memory_write_intent(value: str) -> bool:
    return any(term in value for term in (
        "remember", "save", "store", "记住", "保存", "以后记得", "长期保存",
    ))
