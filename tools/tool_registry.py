import json
import re
from typing import Callable, Any

from tools.policy import UNLOCKED_TOOLS_KEY, ToolPolicy
from tools.spec import ToolExposure, ToolRisk, ToolSpec, ToolStateEffect
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

_PRELOADED_TOOLS = {
    "tool_search",
    "bash",
    "list_files",
    "rg",
    "read_file",
    "read_files",
    "code_outline",
    "write_file",
    "edit_file",
    "idle",
}
_DEFERRED_TOOLS = {
    "background_run", "git_add",
    "git_commit", "spawn_teammate", "list_teammates", "broadcast",
    "shutdown_request", "shutdown_status", "plan_approval", "claim_task",
    "load_skill", "memorize", "recall_memory", "retrieve_tool_result",
}

_DISCOVERY_METADATA: dict[str, dict[str, tuple[str, ...] | str]] = {
    "web_search": {
        "summary": "Search the public web for current information and official sources.",
        "capabilities": ("web search", "internet research", "current information"),
        "aliases": ("search online", "联网搜索", "网络搜索", "网上查"),
        "keywords": ("web", "internet", "latest", "官网", "最新资料"),
    },
    "memorize": {
        "summary": "Save a durable fact or preference to long-term memory.",
        "capabilities": ("memory write", "save preference", "remember fact"),
        "aliases": ("remember", "记住", "保存偏好", "长期保存"),
        "keywords": ("memory", "preference", "long term", "以后记得"),
    },
    "recall_memory": {
        "summary": "Search long-term memory for prior facts and preferences.",
        "capabilities": ("memory recall", "memory search", "prior context"),
        "aliases": (
            "recall", "previous preference", "mentioned before",
            "回忆", "之前说过", "以前偏好",
        ),
        "keywords": ("memory", "previous preference", "长期记忆"),
    },
    "load_skill": {
        "summary": "Discover and load a specialized workflow from an installed skill.",
        "capabilities": ("skill discovery", "specialized workflow", "procedure"),
        "aliases": ("skill", "load workflow", "技能", "流程", "操作指南"),
        "keywords": ("specialized instruction", "适合这个任务", "专业流程"),
    },
    "task": {
        "summary": "Delegate one bounded task to a specialized subagent.",
        "capabilities": ("subagent", "delegate task", "research worker"),
        "aliases": ("delegate", "spawn subagent", "子智能体", "委派", "拆任务"),
        "keywords": ("agent", "worker", "parallel research", "子任务"),
    },
    "parallel_tasks": {
        "summary": "Run multiple independent subagent tasks in parallel.",
        "capabilities": ("parallel subagents", "parallel delegation", "fan out"),
        "aliases": ("parallel agents", "并行子智能体", "并行分析", "并行委派"),
        "keywords": ("subagent", "delegate", "workers", "拆任务"),
    },
    "background_run": {
        "summary": "Start a long-running command as a background task.",
        "capabilities": ("background task", "long running command"),
        "aliases": ("run in background", "后台运行", "后台任务"),
        "keywords": ("async", "long process", "后台"),
    },
    "git_add": {
        "summary": "Stage selected repository changes for a Git commit.",
        "capabilities": ("git stage", "version control write"),
        "aliases": ("stage changes", "stage these changes", "git add", "暂存修改"),
        "keywords": ("git", "index", "commit preparation"),
    },
    "git_commit": {
        "summary": "Create a Git commit from staged changes.",
        "capabilities": ("git commit", "version control write"),
        "aliases": ("commit changes", "提交代码", "创建提交"),
        "keywords": ("git", "commit", "版本控制"),
    },
    "spawn_teammate": {
        "summary": "Start a persistent coding teammate for collaborative work.",
        "capabilities": ("teammate", "collaborative agent", "delegate coding"),
        "aliases": ("spawn teammate", "启动队友", "协作智能体"),
        "keywords": ("agent", "team", "collaboration", "协作"),
    },
    "list_teammates": {
        "summary": "List active collaborative teammates and their status.",
        "capabilities": ("teammate status", "agent roster"),
        "aliases": ("list agents", "队友列表", "智能体状态"),
        "keywords": ("team", "agents", "status"),
    },
    "broadcast": {
        "summary": "Send one message to all active teammates.",
        "capabilities": ("team broadcast", "agent communication"),
        "aliases": ("message all agents", "广播消息", "通知所有队友"),
        "keywords": ("team", "message", "communication"),
    },
    "shutdown_request": {
        "summary": "Request a teammate to stop after completing safe cleanup.",
        "capabilities": ("teammate shutdown", "agent lifecycle"),
        "aliases": ("stop teammate", "关闭队友", "停止智能体"),
        "keywords": ("shutdown", "agent", "team"),
    },
    "shutdown_status": {
        "summary": "Inspect teammate shutdown request status.",
        "capabilities": ("shutdown status", "agent lifecycle status"),
        "aliases": ("check shutdown", "关闭状态"),
        "keywords": ("shutdown", "status", "agent"),
    },
    "plan_approval": {
        "summary": "Approve or reject a teammate's submitted plan.",
        "capabilities": ("plan approval", "teammate governance"),
        "aliases": ("approve plan", "审批计划", "批准方案"),
        "keywords": ("plan", "approval", "team"),
    },
    "claim_task": {
        "summary": "Claim an available team task for execution.",
        "capabilities": ("claim task", "team task coordination"),
        "aliases": ("take task", "领取任务", "认领任务"),
        "keywords": ("task", "team", "claim"),
    },
    "retrieve_tool_result": {
        "summary": "Retrieve a previously externalized large tool result.",
        "capabilities": ("tool result retrieval", "large result access"),
        "aliases": ("retrieve result", "读取工具结果", "取回大结果"),
        "keywords": ("artifact", "tool output", "result reference"),
    },
}


def _builtin_spec(schema: dict, handler: Callable[..., str], *, source: str) -> ToolSpec:
    name = str(schema["function"]["name"])
    non_idempotent = name in _NON_IDEMPOTENT_TOOLS
    if name == "update_task_state":
        exposure = ToolExposure.CONDITIONAL
    elif name in _PRELOADED_TOOLS or (
        name == "send_message" and source.startswith("teammate:")
    ):
        exposure = ToolExposure.PRELOADED
    else:
        exposure = ToolExposure.DEFERRED
    discovery = _DISCOVERY_METADATA.get(name, {})
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
        exposure=exposure,
        discovery_summary=str(discovery.get("summary") or schema["function"].get("description", "")),
        capabilities=tuple(discovery.get("capabilities") or (name.replace("_", " "),)),
        aliases=tuple(discovery.get("aliases") or (name,)),
        keywords=tuple(discovery.get("keywords") or (name.replace("_", " "),)),
        condition="task_state_active" if name == "update_task_state" else "",
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
