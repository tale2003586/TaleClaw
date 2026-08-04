"""Agent execution adapter."""

from types import SimpleNamespace
from typing import Callable

from runtime.agent_spec import AgentSpec
from runtime.ports import ContextPort, ModelPort, ToolExecutorPort, ToolPort
from runtime.execution.reasoning_loop import DEFAULT_MAX_REASONING_STEPS, ReasoningLoop
from runtime.execution.policy_set import ExecutionPolicies
from runtime.execution.state import RunExecutionState


class AgentRunner:
    """Reusable adapter from an AgentSpec to the shared ReasoningLoop."""

    def __init__(
        self,
        *,
        tools: ToolPort,
        tool_executor: ToolExecutorPort,
        provider: ModelPort | None = None,
        model: str = "",
        model_pool=None,
        context_builder: ContextPort | None = None,
        reflection_agent=None,
        max_tokens: int = 8000,
        max_reasoning_steps: int = DEFAULT_MAX_REASONING_STEPS,
        execution_policy_factory=None,
    ) -> None:
        if tool_executor is None:
            raise ValueError("AgentRunner requires a tool_executor.")
        self.tools = tools
        self.tool_executor = tool_executor
        self.provider = provider
        self.model = model
        self.model_pool = model_pool
        self.context_builder = context_builder
        self.reflection_agent = reflection_agent
        self.max_tokens = max_tokens
        self.max_reasoning_steps = max(1, int(max_reasoning_steps))
        self.execution_policy_factory = (
            execution_policy_factory or ExecutionPolicies.minimal
        )

    def run(
        self,
        *,
        session,
        spec: AgentSpec,
        build_context: Callable | None = None,
        after_turn: Callable | None = None,
        after_tool_calls: Callable | None = None,
        on_text: Callable[[str], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        checkpoint_callback: Callable | None = None,
        run_state=None,
        trace_store=None,
        trace_parent_span_id: str | None = None,
        run_context=None,
    ) -> None:
        if run_context is None:
            execution_state = RunExecutionState(
                run_id=str(getattr(run_state, "run_id", "") or ""),
                messages=getattr(session, "messages", []),
            )
            execution_state.reset(web_search_limit=0)
            run_context = SimpleNamespace(state=execution_state)
        context_builder = build_context or self._build_context
        turn_finished = after_turn or self._touch_session
        effective_max_steps = spec.max_reasoning_steps or self.max_reasoning_steps
        policies = self.execution_policy_factory(effective_max_steps)
        loop = ReasoningLoop(
            tools=self.tools,
            tool_executor=self.tool_executor,
            max_tokens=spec.max_tokens or self.max_tokens,
            max_reasoning_steps=effective_max_steps,
            policies=policies,
        )
        loop.run(
            session=session,
            profile=spec.profile or spec,
            build_context=context_builder,
            resolve_provider=lambda session, profile: self._provider_and_model(
                session,
                profile,
                spec,
            ),
            after_turn=turn_finished,
            after_tool_calls=after_tool_calls,
            reflection_agent=self.reflection_agent,
            on_text=on_text,
            cancel_requested=cancel_requested,
            checkpoint_callback=checkpoint_callback,
            run_state=run_state,
            trace_store=trace_store,
            trace_parent_span_id=trace_parent_span_id,
            run_context=run_context,
        )

    def reset_turn_state(self, session) -> None:
        reset_tools = getattr(self.tools, "reset_turn_unlocks", None)
        if reset_tools is not None:
            reset_tools(session)
        reset_executor = getattr(self.tool_executor, "reset_turn", None)
        if reset_executor is not None:
            reset_executor(session.id)

    def _provider_and_model(self, session, profile, spec: AgentSpec):
        return self.provider_and_model_for_purpose(spec.model_purpose or "chat")

    def provider_and_model_for_purpose(self, purpose: str = "chat"):
        if self.model_pool is not None:
            return (
                self.model_pool.routed_provider(purpose),
                self.model_pool.model_for(purpose),
            )
        if self.provider is None:
            raise RuntimeError("AgentRunner has no provider or model_pool.")
        return self.provider, self.model

    def _build_context(self, session, profile, **kwargs):
        if self.context_builder is None:
            raise RuntimeError("AgentRunner has no context_builder.")
        return self.context_builder.build(session=session, profile=profile, **kwargs)

    def _touch_session(self, session) -> None:
        session.touch()
