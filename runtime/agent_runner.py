from typing import Callable

from runtime.agent_spec import AgentSpec
from runtime.reasoning_loop import DEFAULT_MAX_REASONING_STEPS, ReasoningLoop


class AgentRunner:
    """Reusable adapter from an AgentSpec to the shared ReasoningLoop."""

    def __init__(
        self,
        *,
        tools,
        tool_executor,
        provider=None,
        model: str = "",
        model_pool=None,
        context_builder=None,
        reflection_agent=None,
        max_tokens: int = 8000,
        max_reasoning_steps: int = DEFAULT_MAX_REASONING_STEPS,
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

    def run_turn(
        self,
        *,
        session,
        spec: AgentSpec,
        build_context: Callable | None = None,
        after_turn: Callable | None = None,
        after_tool_calls: Callable | None = None,
        on_text: Callable[[str], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        run_state=None,
        trace_store=None,
        trace_parent_span_id: str | None = None,
    ) -> None:
        context_builder = build_context or self._build_context
        turn_finished = after_turn or self._touch_session
        loop = ReasoningLoop(
            tools=self.tools,
            tool_executor=self.tool_executor,
            max_tokens=spec.max_tokens or self.max_tokens,
            max_reasoning_steps=(
                spec.max_reasoning_steps or self.max_reasoning_steps
            ),
        )
        loop.run(
            session=session,
            profile=spec.profile,
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
            run_state=run_state,
            trace_store=trace_store,
            trace_parent_span_id=trace_parent_span_id,
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

    def _build_context(self, session, profile):
        if self.context_builder is None:
            raise RuntimeError("AgentRunner has no context_builder.")
        return self.context_builder.build(session=session, profile=profile)

    def _touch_session(self, session) -> None:
        session.touch()
