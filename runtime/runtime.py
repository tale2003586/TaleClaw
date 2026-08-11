from dataclasses import dataclass, field
from typing import Any

from runtime.execution.agent_runner import AgentRunner
from runtime.execution.state import RunExecutionState
from runtime.agent_spec import AgentSpec
from runtime.context import ContextBundle
from runtime.extensions import ContextContribution, RuntimeExtensions
from runtime.ports import ContextPort, LifecyclePort, ModelPort, ToolExecutorPort, ToolPort
from runtime.execution.failure_reasons import (
    StopReason,
)
from runtime.execution.reasoning_loop import (
    DEFAULT_MAX_REASONING_STEPS,
)
from typing import Callable


DEFAULT_WEB_SEARCH_BUDGET = 6


class Runtime:
    def __init__(
        self,
        tools: ToolPort,
        provider: ModelPort | None,
        model: str,
        tool_executor: ToolExecutorPort | None = None,
        context_builder: ContextPort | None = None,
        memory_lifecycle: LifecyclePort | None = None,
        model_pool=None,
        reflection_agent=None,
        execution_policy_factory=None,
        max_tokens: int = 8000,
        max_reasoning_steps: int = DEFAULT_MAX_REASONING_STEPS,
    ) -> None:
        if tool_executor is None:
            raise ValueError("Runtime requires a tool_executor.")
        if context_builder is None:
            raise ValueError("Runtime requires a context_builder.")
        self.memory_lifecycle = memory_lifecycle
        self.execution_policy_factory = execution_policy_factory
        self.agent_runner = AgentRunner(
            tools=tools,
            tool_executor=tool_executor,
            provider=provider,
            model=model,
            model_pool=model_pool,
            context_builder=context_builder,
            reflection_agent=reflection_agent,
            execution_policy_factory=execution_policy_factory,
            max_tokens=max_tokens,
            max_reasoning_steps=max_reasoning_steps,
        )

    @property
    def max_tokens(self) -> int:
        return self.agent_runner.max_tokens

    @property
    def max_reasoning_steps(self) -> int:
        return self.agent_runner.max_reasoning_steps

    def fork(
        self,
        *,
        context_builder,
        tools=None,
        memory_lifecycle=None,
        max_reasoning_steps: int | None = None,
        execution_policy_factory=None,
    ) -> "Runtime":
        return Runtime(
            tools=tools or self.agent_runner.tools,
            provider=self.agent_runner.provider,
            model=self.agent_runner.model,
            tool_executor=self.agent_runner.tool_executor,
            context_builder=context_builder,
            memory_lifecycle=memory_lifecycle,
            model_pool=self.agent_runner.model_pool,
            reflection_agent=self.agent_runner.reflection_agent,
            execution_policy_factory=(
                execution_policy_factory or self.execution_policy_factory
            ),
            max_tokens=self.agent_runner.max_tokens,
            max_reasoning_steps=max_reasoning_steps or self.agent_runner.max_reasoning_steps,
        )

    def provider_and_model_for(self, purpose: str = "chat"):
        return self.agent_runner.provider_and_model_for_purpose(purpose)

    def run(self, agent: AgentSpec, user_input: str, context: "RunContext") -> "RunResult":
        """Execute one agent turn through the sole public Runtime entry."""
        if not isinstance(agent, AgentSpec):
            raise TypeError("Runtime.run agent must be an AgentSpec.")
        if not isinstance(context, RunContext):
            raise TypeError("Runtime.run context must be a RunContext.")
        context.state.input_text = user_input
        context.state.messages = context.session.messages
        profile = context.profile or agent.profile or agent
        self._execute(agent=agent, profile=profile, context=context)
        output = get_last_assistant_text(context.session.messages)
        return RunResult(
            output=str(output or ""),
            session=context.session,
            agent=agent,
            execution=context.state,
            run_state=context.run_state,
        )

    def _execute(self, *, agent: AgentSpec, profile: Any, context: "RunContext") -> None:
        session = context.session
        active_turn_start_index = _last_user_message_index(session.messages)
        self._before_turn(session, run_context=context)
        context_prefix = self._build_context_prefix(
            session,
            profile,
            active_turn_start_index=active_turn_start_index,
        )
        self.agent_runner.run(
            session=session,
            spec=agent,
            build_context=lambda session, profile, **trace_kwargs: self._before_reasoning(
                session,
                profile,
                active_turn_start_index=active_turn_start_index,
                context_prefix=context_prefix,
                context_policy=agent.context_policy,
                run_context=context,
                **trace_kwargs,
            ),
            after_turn=lambda session: self._after_turn(
                session,
                run_state=context.run_state,
                trace_store=context.trace_store,
            ),
            on_text=context.on_text,
            cancel_requested=context.cancel_requested,
            checkpoint_callback=context.checkpoint_callback,
            run_state=context.run_state,
            trace_store=context.trace_store,
            trace_parent_span_id=context.trace_parent_span_id,
            run_context=context,
        )

    def _before_turn(self, session, *, run_context=None) -> None:
        self.agent_runner.reset_turn_state(session)
        state = getattr(run_context, "state", None)
        if state is not None:
            state.reset(web_search_limit=DEFAULT_WEB_SEARCH_BUDGET)

    def _build_context_prefix(
        self,
        session,
        profile,
        *,
        active_turn_start_index=None,
    ):
        context_builder = self.agent_runner.context_builder
        return context_builder.build_prefix(
            profile,
            session=session,
            active_turn_start_index=active_turn_start_index,
        )

    def _before_reasoning(
        self,
        session,
        profile,
        *,
        active_turn_start_index=None,
        context_prefix=None,
        trace_store=None,
        run_state=None,
        trace_parent_span_id: str | None = None,
        reasoning_step: int | None = None,
        run_context=None,
        context_policy=None,
        model_provider=None,
        model_tools=None,
        reserved_output_tokens: int = 0,
    ):
        state = getattr(run_context, "state", None)
        already_used = state.security_knowledge_used if state is not None else False
        include_security_knowledge = not already_used
        contributions: list[ContextContribution] = []
        for contributor in run_context.extensions.context_contributors:
            contributions.extend(contributor.contribute(session=session, profile=profile))
        build_kwargs = {
            "session": session,
            "profile": profile,
            "active_turn_start_index": active_turn_start_index,
            "include_security_knowledge": include_security_knowledge,
            "contributions": contributions,
            "context_policy": context_policy,
            "trace_store": trace_store,
            "run_state": run_state,
            "trace_parent_span_id": trace_parent_span_id,
            "reasoning_step": reasoning_step,
            "model_provider": model_provider,
            "model_tools": model_tools,
            "reserved_output_tokens": reserved_output_tokens,
        }
        if context_prefix is not None:
            build_kwargs["prefix"] = context_prefix
        context = self.agent_runner.context_builder.build(**build_kwargs)
        if _section_rendered(context, "security_knowledge"):
            if state is not None:
                state.security_knowledge_used = True
        return self._with_tool_catalog(context, session, profile)

    def _with_tool_catalog(self, context, session, profile):
        catalog = self._tool_catalog(session, profile)
        if not catalog:
            return context
        messages = list(getattr(context, "messages", []) or [])
        messages.insert(_active_turn_insert_index(context, messages), {
            "role": "user",
            "content": catalog,
        })
        return ContextBundle(
            messages=messages,
            report=getattr(context, "report", None),
        )

    def _tool_catalog(self, session, profile) -> str:
        tools = self.agent_runner.tools
        render = getattr(tools, "tool_catalog_text", None)
        if render is None:
            return ""
        return render(
            session,
            str(getattr(profile, "tool_mode", "bot") or "bot"),
        )

    def _after_turn(self, session, *, run_state=None, trace_store=None) -> None:
        queued_memory_events = []
        metadata = getattr(session, "metadata", None)
        if isinstance(metadata, dict):
            queued_memory_events = list(metadata.pop("memory_trace_events", []) or [])
        if trace_store is not None and run_state is not None:
            for item in queued_memory_events:
                event_name = item.get("event") if isinstance(item, dict) else ""
                payload = item.get("payload") if isinstance(item, dict) else {}
                if event_name:
                    trace_store.append_event(run_state, event_name, payload or {})
        if self.memory_lifecycle is not None:
            enqueue = getattr(self.memory_lifecycle, "enqueue_after_turn", None)
            if enqueue is not None:
                enqueue(session, run_state=run_state, trace_store=trace_store)
            else:
                result = self.memory_lifecycle.after_turn(session)
                if trace_store is not None and run_state is not None and result is not None:
                    for item in getattr(result, "trace_events", []) or []:
                        event_name = item.get("event")
                        payload = item.get("payload") or {}
                        if event_name:
                            trace_store.append_event(run_state, event_name, payload)
                    trace_store.append_event(
                        run_state,
                        "memory.lifecycle.completed",
                        result.to_trace_payload()
                        if hasattr(result, "to_trace_payload")
                        else {},
                    )
        session.touch()

def get_last_assistant_text(messages: list) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant":
            return message_text(message)
    return ""


def message_text(message: dict) -> str:
    """Extract text content from a message (handles various formats)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            elif hasattr(block, "text"):
                parts.append(block.text)
        return "".join(parts)
    return ""


def _last_user_message_index(messages: list) -> int | None:
    for index in range(len(messages or []) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "user":
            return index
    return None


def _active_turn_insert_index(context, messages: list) -> int:
    report = getattr(context, "report", None)
    if report is None:
        return len(messages)
    try:
        sections = report.to_dict().get("sections", {})
    except AttributeError:
        return len(messages)
    active = sections.get("active_turn") or {}
    metadata = active.get("metadata") or {}
    try:
        active_count = int(metadata.get("message_count") or 0)
    except (TypeError, ValueError):
        active_count = 0
    try:
        active_count = int(metadata.get("rendered_message_count") or active_count)
    except (TypeError, ValueError):
        pass
    if active_count <= 0:
        return len(messages)
    return max(1, len(messages) - active_count)


def _section_rendered(context, name: str) -> bool:
    report = getattr(context, "report", None)
    if report is None:
        return False
    try:
        sections = report.to_dict().get("sections", {})
    except AttributeError:
        return False
    section = sections.get(name) or {}
    try:
        return int(section.get("rendered_chars") or 0) > 0
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class RunContext:
    session: Any
    profile: Any = None
    on_text: Callable[[str], None] | None = None
    cancel_requested: Callable[[], bool] | None = None
    checkpoint_callback: Callable | None = None
    run_state: Any = None
    trace_store: Any = None
    trace_parent_span_id: str | None = None
    dependencies: dict[str, Any] = field(default_factory=dict)
    extensions: RuntimeExtensions = field(default_factory=RuntimeExtensions)
    state: RunExecutionState = field(default_factory=RunExecutionState)

    def __post_init__(self) -> None:
        if self.run_state is None:
            return
        if not self.state.run_id:
            self.state.run_id = str(getattr(self.run_state, "run_id", "") or "")
        if not self.state.parent_run_id:
            metadata = getattr(self.run_state, "metadata", {}) or {}
            self.state.parent_run_id = str(metadata.get("parent_run_id", "") or "")


@dataclass(frozen=True)
class RunResult:
    output: str
    session: Any
    agent: AgentSpec
    execution: RunExecutionState
    run_state: Any = None
