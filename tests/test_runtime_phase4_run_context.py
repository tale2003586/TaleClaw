from __future__ import annotations

from types import SimpleNamespace

from agents.definitions import BOT_AGENT_SPEC
from runtime.context import ContextBundle
from runtime.execution.failure_reasons import REASONING_LOOP_STOP_REASON_KEY, StopReason
from runtime.execution.loop_policies import WebSearchBudgetPolicy
from runtime.runtime import Runtime
from runtime.runtime import RunContext, RunExecutionState, Runtime
from runtime.trace.run_state import RunState
from runtime.sessions import Session
from tests.fakes.scripted_model import FinalResponse, ScriptedModel
from tools.executor import ToolExecutor
from tools.tool_registry import ToolRegistry


def _pipeline(response="done"):
    return Runtime(
        tools=ToolRegistry(),
        provider=ScriptedModel([FinalResponse(response)]),
        model="fake",
        tool_executor=ToolExecutor([]),
        context_builder=SimpleNamespace(
            build=lambda *, session, profile, **kwargs: ContextBundle(
                messages=[
                    {"role": "system", "content": profile.system_prompt},
                    *session.messages,
                ]
            )
        ),
        memory_lifecycle=None,
        max_tokens=128,
    )


def test_run_context_derives_run_identity_and_keeps_state_out_of_agent_spec():
    run_state = RunState.create(
        run_id="phase4-run",
        session_id="phase4:identity",
        metadata={"parent_run_id": "phase4-parent"},
    )
    context = RunContext(
        session=Session(id="phase4:identity"),
        run_state=run_state,
    )

    assert context.state.run_id == "phase4-run"
    assert context.state.parent_run_id == "phase4-parent"
    assert not hasattr(BOT_AGENT_SPEC, "run_id")


def test_runtime_records_current_input_and_messages_in_explicit_state():
    session = Session(id="phase4:input")
    session.add_message("user", "hello")
    context = RunContext(session=session)

    _pipeline().run(BOT_AGENT_SPEC, "hello", context)

    assert context.state.input_text == "hello"
    assert context.state.messages is session.messages


def test_runtime_double_writes_turn_budget_state_and_legacy_metadata():
    session = Session(id="phase4:budget")
    session.add_message("user", "hello")
    context = RunContext(session=session)

    _pipeline().run(BOT_AGENT_SPEC, "hello", context)

    assert context.state.web_search_limit == 6
    assert context.state.web_search_used == 0
    assert context.state.web_search_remaining == 6
    assert session.metadata["web_search_budget_limit"] == 6
    assert session.metadata["web_search_budget_used"] == 0
    assert session.metadata["web_search_budget_remaining"] == 6


def test_web_search_policy_updates_explicit_and_legacy_state_together():
    session = Session(
        id="phase4:web",
        metadata={
            "web_search_budget_limit": 2,
            "web_search_budget_used": 0,
            "web_search_budget_remaining": 2,
        },
    )
    state = RunExecutionState(
        web_search_limit=2,
        web_search_used=0,
        web_search_remaining=2,
    )

    assert WebSearchBudgetPolicy().denial(
        session,
        "web_search",
        state=state,
    ) == ""

    assert state.web_search_used == session.metadata["web_search_budget_used"] == 1
    assert (
        state.web_search_remaining
        == session.metadata["web_search_budget_remaining"]
        == 1
    )


def test_cancellation_stop_state_is_double_written():
    session = Session(id="phase4:cancel")
    session.add_message("user", "cancel")
    context = RunContext(
        session=session,
        cancel_requested=lambda: True,
    )

    _pipeline("unused").run(
        BOT_AGENT_SPEC,
        "cancel",
        context,
    )

    assert context.state.stop_reason == StopReason.USER_CANCELLED.value
    assert session.metadata[REASONING_LOOP_STOP_REASON_KEY] == context.state.stop_reason
    assert context.state.stop_message
