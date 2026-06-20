from coding_runtime.background_task import BG
from bus.team_bus import BUS
from runtime.agent_runner import AgentRunner
from runtime.agent_spec import AgentSpec
from runtime.context import ContextBundle
from runtime.failure_reasons import (
    REASONING_LOOP_STOP_MESSAGE_KEY,
    REASONING_LOOP_STOP_REASON_KEY,
)
from runtime.reasoning_loop import (
    DEFAULT_MAX_REASONING_STEPS,
)
from typing import Callable


SECURITY_RAG_AUTO_CONTEXT_USED_KEY = "security_rag_auto_context_used"
WEB_SEARCH_BUDGET_LIMIT_KEY = "web_search_budget_limit"
WEB_SEARCH_BUDGET_USED_KEY = "web_search_budget_used"
WEB_SEARCH_BUDGET_REMAINING_KEY = "web_search_budget_remaining"
DEFAULT_WEB_SEARCH_BUDGET = 6


class Pipeline:
    def __init__(
        self,
        tools,
        provider,
        model: str,
        tool_executor=None,
        context_builder=None,
        memory_lifecycle=None,
        model_pool=None,
        reflection_agent=None,
        max_tokens: int = 8000,
        max_reasoning_steps: int = DEFAULT_MAX_REASONING_STEPS,
    ) -> None:
        if tool_executor is None:
            raise ValueError("Pipeline requires a tool_executor.")
        if context_builder is None:
            raise ValueError("Pipeline requires a context_builder.")
        self.memory_lifecycle = memory_lifecycle
        self.agent_runner = AgentRunner(
            tools=tools,
            tool_executor=tool_executor,
            provider=provider,
            model=model,
            model_pool=model_pool,
            context_builder=context_builder,
            reflection_agent=reflection_agent,
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
        memory_lifecycle=None,
        max_reasoning_steps: int | None = None,
    ) -> "Pipeline":
        return Pipeline(
            tools=self.agent_runner.tools,
            provider=self.agent_runner.provider,
            model=self.agent_runner.model,
            tool_executor=self.agent_runner.tool_executor,
            context_builder=context_builder,
            memory_lifecycle=memory_lifecycle,
            model_pool=self.agent_runner.model_pool,
            reflection_agent=self.agent_runner.reflection_agent,
            max_tokens=self.agent_runner.max_tokens,
            max_reasoning_steps=max_reasoning_steps or self.agent_runner.max_reasoning_steps,
        )

    def provider_and_model_for(self, purpose: str = "chat"):
        return self.agent_runner.provider_and_model_for_purpose(purpose)

    def run(
        self,
        session,
        profile,
        on_text: Callable[[str], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        run_state=None,
        trace_store=None,
        trace_parent_span_id: str | None = None,
    ) -> str:
        self._run_turn(
            session,
            profile,
            on_text=on_text,
            cancel_requested=cancel_requested,
            run_state=run_state,
            trace_store=trace_store,
            trace_parent_span_id=trace_parent_span_id,
        )
        return get_last_assistant_text(session.messages)

    def _run_turn(
        self,
        session,
        profile,
        on_text: Callable[[str], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        run_state=None,
        trace_store=None,
        trace_parent_span_id: str | None = None,
    ) -> None:
        active_turn_start_index = _last_user_message_index(session.messages)
        self._before_turn(session)
        self.agent_runner.run_turn(
            session=session,
            spec=self._agent_spec(session, profile),
            build_context=lambda session, profile: self._before_reasoning(
                session,
                profile,
                active_turn_start_index=active_turn_start_index,
            ),
            after_turn=lambda session: self._after_turn(
                session,
                run_state=run_state,
                trace_store=trace_store,
            ),
            on_text=on_text,
            cancel_requested=cancel_requested,
            run_state=run_state,
            trace_store=trace_store,
            trace_parent_span_id=trace_parent_span_id,
        )

    def _before_turn(self, session) -> None:
        self.agent_runner.reset_turn_state(session)
        session.metadata.pop(REASONING_LOOP_STOP_REASON_KEY, None)
        session.metadata.pop(REASONING_LOOP_STOP_MESSAGE_KEY, None)
        session.metadata[SECURITY_RAG_AUTO_CONTEXT_USED_KEY] = False
        session.metadata[WEB_SEARCH_BUDGET_LIMIT_KEY] = DEFAULT_WEB_SEARCH_BUDGET
        session.metadata[WEB_SEARCH_BUDGET_USED_KEY] = 0
        session.metadata[WEB_SEARCH_BUDGET_REMAINING_KEY] = DEFAULT_WEB_SEARCH_BUDGET

    def _before_reasoning(self, session, profile, *, active_turn_start_index=None):
        if self._should_include_task_runtime_events(session, profile):
            notifs = BG.drain_notifications()
            inbox = BUS.read_inbox("lead")
        else:
            notifs = []
            inbox = []

        include_security_knowledge = not bool(
            session.metadata.get(SECURITY_RAG_AUTO_CONTEXT_USED_KEY)
        )
        context = self.agent_runner.context_builder.build(
            session=session,
            profile=profile,
            inbox=inbox,
            background_results=notifs,
            active_turn_start_index=active_turn_start_index,
            include_security_knowledge=include_security_knowledge,
        )
        if _section_rendered(context, "security_knowledge"):
            session.metadata[SECURITY_RAG_AUTO_CONTEXT_USED_KEY] = True
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

    def _agent_spec(self, session, profile) -> AgentSpec:
        return AgentSpec(
            name=str(getattr(profile, "name", "main") or "main"),
            profile=profile,
            model_purpose=self._model_purpose(session, profile),
            max_tokens=self.agent_runner.max_tokens,
            max_reasoning_steps=self.agent_runner.max_reasoning_steps,
        )

    def _model_purpose(self, session, profile) -> str:
        if profile.tool_mode == "coding":
            return "coding"
        return "chat"

    def _after_turn(self, session, *, run_state=None, trace_store=None) -> None:
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

    def _should_include_task_runtime_events(self, session, profile) -> bool:
        metadata = session.metadata or {}
        return (
            metadata.get("kind") == "task_session"
            and profile.tool_mode == "coding"
        )


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
