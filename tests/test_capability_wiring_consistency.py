from pathlib import Path
from types import SimpleNamespace

from agents.definitions import BOT_AGENT_SPEC, CODING_AGENT_SPEC
from agents.subagent.runner import TaskSubagentRunner
from agents.subagent.tools import SUBTASK_SYSTEM_PROMPTS, SUBTASK_TOOL_WHITELIST
from models.provider import LLMResponse, ToolCall
from plugins.plugin_manager import PluginManager
from plugins.web_search.plugin import WebSearchPlugin
from runtime.agent_spec import (
    AgentSpec,
    ContextPolicy,
    RunLimits,
    SpawnPolicy,
    TerminationPolicy,
    ToolSet,
)
from runtime.execution.agent_runner import AgentRunner
from runtime.execution.failure_reasons import StopReason
from runtime.execution.state import RunExecutionState
from runtime.sessions import Session
from runtime.task_state import ensure_task_state_core
from skill_runtime import SKILL_LOADER
from tools.executor import ToolExecutor
from tools.schema import function_tool
from tools.spec import ToolExposure, ToolSpec
from tools.tool_registry import ToolRegistry, build_lead_tool_registry


class _ContextBuilder:
    def build(self, **kwargs):
        return SimpleNamespace(messages=kwargs["session"].messages)


class _Provider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _tool_response(index: int, name: str = "echo") -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id=f"call-{index}", name=name, arguments={})],
        raw_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"call-{index}",
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }],
        },
    )


def _spec(name: str, exposure: ToolExposure, **kwargs) -> ToolSpec:
    return ToolSpec(
        schema=function_tool(name, f"Use {name} capability.", {}, []),
        handler=lambda **_: "ok",
        allowed_modes=frozenset({"coding"}),
        exposure=exposure,
        discovery_summary=f"Discover {name} capability.",
        aliases=(f"find {name}",),
        capabilities=(name,),
        **kwargs,
    )


def _register_search(registry: ToolRegistry) -> None:
    registry.register(_spec("tool_search", ToolExposure.PRELOADED))


def test_exposure_states_are_runtime_authoritative() -> None:
    registry = ToolRegistry()
    _register_search(registry)
    registry.register(_spec("preloaded", ToolExposure.PRELOADED))
    registry.register(_spec("deferred", ToolExposure.DEFERRED))
    registry.register(_spec(
        "conditional",
        ToolExposure.CONDITIONAL,
        condition="task_state_active",
    ))
    registry.register(_spec("internal", ToolExposure.INTERNAL))
    session = Session("exposure")

    assert registry.visible_names_for_turn(session, "coding") == {"preloaded", "tool_search"}
    result = registry.execute(
        "tool_search",
        {"query": "find deferred"},
        session=session,
        mode="coding",
    )
    assert "deferred" in result
    assert "deferred" in registry.visible_names_for_turn(session, "coding")
    assert "internal" not in result
    ensure_task_state_core(session, objective="long task")
    assert "conditional" in registry.visible_names_for_turn(session, "coding")


def test_agent_tool_set_allow_and_deny_change_the_final_view() -> None:
    registry = build_lead_tool_registry()
    session = Session("tool-set")
    allow = AgentSpec(
        name="allow",
        tool_set=ToolSet(mode="coding", allow=("tool_search", "read_file")),
    )
    deny = AgentSpec(
        name="deny",
        tool_set=ToolSet(mode="coding", deny=("bash",)),
    )

    assert registry.visible_names_for_turn(
        session, "coding", agent_spec=allow
    ) == {"tool_search", "read_file"}
    assert "bash" not in registry.visible_names_for_turn(
        session, "coding", agent_spec=deny
    )
    deny_search = AgentSpec(
        name="deny-search",
        tool_set=ToolSet(mode="coding", deny=("tool_search",)),
    )
    assert "not visible" in registry.execute(
        "tool_search",
        {"query": "git status"},
        session=Session("deny-search"),
        mode="coding",
        agent_spec=deny_search,
    )


def test_tool_allowed_agent_types_filters_visibility_before_discovery() -> None:
    registry = ToolRegistry()
    _register_search(registry)
    registry.register(_spec(
        "scout_only",
        ToolExposure.DEFERRED,
        allowed_agent_types=frozenset({"explore"}),
    ))
    explore = AgentSpec(
        name="subagent:explore",
        tool_set=ToolSet(mode="coding"),
        metadata={"agent_type": "explore"},
    )
    code = AgentSpec(
        name="subagent:code",
        tool_set=ToolSet(mode="coding"),
        metadata={"agent_type": "code"},
    )
    assert "scout_only" in registry.execute(
        "tool_search",
        {"query": "find scout only"},
        session=Session("explore"),
        mode="coding",
        agent_spec=explore,
    )
    assert "No matching" in registry.execute(
        "tool_search",
        {"query": "find scout only"},
        session=Session("code"),
        mode="coding",
        agent_spec=code,
    )


def test_capability_search_recovers_lists_and_auto_unlocks_chinese_intents() -> None:
    registry = build_lead_tool_registry()
    catalog = registry.execute(
        "tool_search", {"query": "catalog"}, session=Session("catalog"), mode="bot"
    )
    assert "Available capability groups" in catalog
    cases = (
        ("记住我以后 Python 测试默认使用 pytest", "memorize"),
        ("我之前说过测试框架偏好吗？", "recall_memory"),
        ("用适合这个任务的 skill 来处理", "load_skill"),
        ("开几个子 agent 并行分析", "parallel_tasks"),
    )
    for index, (query, expected) in enumerate(cases):
        session = Session(f"intent:{index}", metadata={"user_role": "admin"})
        result = registry.execute(
            "tool_search",
            {"query": query},
            session=session,
            mode="coding" if expected in {"load_skill", "parallel_tasks"} else "bot",
            agent_spec=CODING_AGENT_SPEC if expected in {"load_skill", "parallel_tasks"} else BOT_AGENT_SPEC,
        )
        assert expected in result
        assert "select:" not in result
        assert expected in session.metadata["unlocked_tools"]


def test_web_plugin_is_deferred_but_discoverable_by_chinese_intent(tmp_path: Path) -> None:
    registry = build_lead_tool_registry()
    PluginManager([WebSearchPlugin()], workspace=tmp_path, tool_registry=registry)
    session = Session("web", metadata={"user_role": "admin"})

    assert "web_search" not in registry.visible_names_for_turn(
        session, "coding", agent_spec=CODING_AGENT_SPEC
    )
    result = registry.execute(
        "tool_search",
        {"query": "帮我联网查一下 OpenAI 最新文档"},
        session=session,
        mode="coding",
        agent_spec=CODING_AGENT_SPEC,
    )
    assert "web_search" in result
    assert "web_search" in registry.visible_names_for_turn(
        session, "coding", agent_spec=CODING_AGENT_SPEC
    )


def test_skill_scope_and_include_skills_are_enforced_for_discovery_and_load() -> None:
    registry = build_lead_tool_registry()
    scoped = AgentSpec(
        name="reviewer",
        tool_set=ToolSet(mode="coding"),
        skills=("code-review",),
    )
    disabled = AgentSpec(
        name="no-skills",
        tool_set=ToolSet(mode="coding"),
        context_policy=ContextPolicy(include_skills=False),
    )
    session = Session("skills")
    result = registry.execute(
        "tool_search",
        {"query": "review this code for bugs"},
        session=session,
        mode="coding",
        agent_spec=scoped,
    )
    assert "skill:code-review" in result
    assert "skill:agent-builder" not in result
    loaded = registry.execute(
        "load_skill",
        {"name": "code-review"},
        session=session,
        mode="coding",
        agent_spec=scoped,
    )
    assert '<skill name="code-review">' in loaded
    denied = registry.execute(
        "load_skill",
        {"name": "agent-builder"},
        session=session,
        mode="coding",
        agent_spec=scoped,
    )
    assert "outside this AgentSpec skill scope" in denied
    assert "load_skill" not in registry.policy._allowed_names(
        session=session, mode="coding", agent_spec=disabled
    )


def test_spawn_policy_is_enforced_beyond_tool_visibility() -> None:
    registry = build_lead_tool_registry()
    session = Session("spawn", metadata={"unlocked_tools": ["task"], "user_role": "admin"})
    disabled = AgentSpec(
        name="disabled",
        tool_set=ToolSet(mode="coding"),
        spawn_policy=SpawnPolicy(enabled=False),
    )
    restricted = AgentSpec(
        name="restricted",
        tool_set=ToolSet(mode="coding"),
        spawn_policy=SpawnPolicy(enabled=True, allowed_agent_types=("explore",)),
    )

    assert "disabled" in registry.execution_error_for_turn(
        "task",
        args={"agent_type": "explore"},
        session=session,
        mode="coding",
        agent_spec=disabled,
    )
    assert "does not allow" in registry.execution_error_for_turn(
        "task",
        args={"agent_type": "code"},
        session=session,
        mode="coding",
        agent_spec=restricted,
    )


def test_max_tool_calls_is_a_runtime_hard_limit() -> None:
    registry = ToolRegistry()
    registry.register(_spec("echo", ToolExposure.PRELOADED))
    provider = _Provider([_tool_response(1), _tool_response(2)])
    state = RunExecutionState()
    runner = AgentRunner(
        tools=registry,
        tool_executor=ToolExecutor([]),
        provider=provider,
        model="test",
        context_builder=_ContextBuilder(),
        max_reasoning_steps=5,
    )
    session = Session("limit")
    session.add_message("user", "run tools")
    spec = AgentSpec(
        name="limited",
        tool_set=ToolSet(mode="coding"),
        limits=RunLimits(max_reasoning_steps=5, max_tool_calls=1),
    )
    runner.run(
        session=session,
        spec=spec,
        run_context=SimpleNamespace(state=state, extensions=SimpleNamespace(run_observers=())),
    )

    assert state.stop_reason == StopReason.TOOL_CALL_LIMIT_EXCEEDED.value
    assert sum(message.get("role") == "tool" for message in session.messages) == 1


def test_allow_empty_final_changes_runtime_termination_behavior() -> None:
    provider = _Provider([LLMResponse(content="", raw_message={})])
    state = RunExecutionState()
    runner = AgentRunner(
        tools=ToolRegistry(),
        tool_executor=ToolExecutor([]),
        provider=provider,
        model="test",
        context_builder=_ContextBuilder(),
    )
    session = Session("empty-final")
    session.add_message("user", "nothing else")
    runner.run(
        session=session,
        spec=AgentSpec(
            name="empty-ok",
            tool_set=ToolSet(mode="coding"),
            termination_policy=TerminationPolicy(allow_empty_final=True),
        ),
        run_context=SimpleNamespace(
            state=state,
            extensions=SimpleNamespace(run_observers=()),
        ),
    )
    assert state.stop_reason == StopReason.COMPLETED.value


def test_capability_discovery_emits_observable_trace() -> None:
    class TraceStore:
        def __init__(self):
            self.events = []

        def append_event(self, _run_state, event, payload, **_kwargs):
            self.events.append((event, payload))

    registry = build_lead_tool_registry()
    trace = TraceStore()
    session = Session("trace")
    registry.execute(
        "tool_search",
        {"query": "remember this preference"},
        session=session,
        mode="bot",
        agent_spec=BOT_AGENT_SPEC,
        trace_store=trace,
        run_state=SimpleNamespace(),
    )
    event, payload = trace.events[-1]
    assert event == "capability.discovery"
    assert payload["query"] == "remember this preference"
    assert payload["matched_tools"][0]["name"] == "memorize"
    assert payload["unlocked"] == ["memorize"]
    assert payload["filters_applied"]


def test_explore_prompt_references_only_registered_and_allowed_tools() -> None:
    registry = build_lead_tool_registry()
    prompt = SUBTASK_SYSTEM_PROMPTS["explore"]
    referenced = {
        name for name in registry._tools
        if name in prompt
    }
    assert {"repo_map", "list_files", "rg", "code_outline", "read_files", "read_file"} <= referenced
    assert referenced <= SUBTASK_TOOL_WHITELIST["explore"]


def test_tool_description_references_are_available_or_discoverable() -> None:
    registry = build_lead_tool_registry()
    dependencies = {
        "read_file": {"read_files"},
        "repo_map": {"code_outline", "read_file"},
    }
    for owner, referenced in dependencies.items():
        assert owner in registry._tools
        assert referenced <= registry.policy._allowed_names(mode="coding")


def test_all_deferred_tools_have_discovery_metadata_and_internal_tools_do_not_leak() -> None:
    registry = build_lead_tool_registry()
    for tool in registry._tools.values():
        if tool.exposure is ToolExposure.DEFERRED:
            assert tool.discovery_summary
            assert tool.capabilities
            assert tool.aliases
            assert tool.keywords
        if tool.exposure is ToolExposure.INTERNAL:
            assert tool.name not in registry.policy._allowed_names(mode="coding")


def test_installed_skill_metadata_is_the_discovery_source() -> None:
    matches = SKILL_LOADER.search("audit this code", mode="coding")
    assert matches[0]["name"] == "code-review"


def test_agent_spec_has_no_declaration_only_fields() -> None:
    assert "hooks" not in AgentSpec.__dataclass_fields__
    assert "output_schema" not in AgentSpec.__dataclass_fields__
    assert "name" not in ContextPolicy.__dataclass_fields__
    assert "name" not in TerminationPolicy.__dataclass_fields__
