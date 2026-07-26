from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.definitions import BOT_AGENT_SPEC
from runtime.runtime import RunContext, Runtime
from runtime.sessions import Session


class RecordingRuntime(Runtime):
    def __init__(self):
        self.calls = []

    def run_turn(self, session, profile, **kwargs):
        self.calls.append({
            "session": session,
            "profile": profile,
            **kwargs,
        })
        session.add_message("assistant", "facade reply")
        return "facade reply"


def test_runtime_run_delegates_all_transient_inputs_without_mutating_input():
    pipeline = RecordingRuntime()
    runtime = pipeline
    session = Session(id="phase2:facade")
    session.add_message("user", "already recorded")
    chunks = []
    cancel = lambda: False
    checkpoint = lambda value: None
    run_state = SimpleNamespace(run_id="run-phase2")
    trace = object()

    context = RunContext(
        session=session,
        on_text=chunks.append,
        cancel_requested=cancel,
        checkpoint_callback=checkpoint,
        run_state=run_state,
        trace_store=trace,
        trace_parent_span_id="parent",
    )
    result = runtime.run(
        BOT_AGENT_SPEC,
        "already recorded",
        context,
    )

    assert result.output == "facade reply"
    assert result.session is session
    assert result.agent is BOT_AGENT_SPEC
    assert pipeline.calls == [{
        "session": session,
        "profile": BOT_AGENT_SPEC,
        "on_text": chunks.append,
        "cancel_requested": cancel,
        "checkpoint_callback": checkpoint,
        "run_state": run_state,
        "trace_store": trace,
        "trace_parent_span_id": "parent",
        "agent_spec": BOT_AGENT_SPEC,
        "run_context": context,
    }]
    assert [message["content"] for message in session.messages] == [
        "already recorded",
        "facade reply",
    ]


def test_runtime_requires_agent_context_and_compatibility_profile():
    runtime = RecordingRuntime()
    with pytest.raises(TypeError, match="AgentSpec"):
        runtime.run(object(), "hello", RunContext(session=Session(id="x")))
    with pytest.raises(TypeError, match="RunContext"):
        runtime.run(BOT_AGENT_SPEC, "hello", object())


def test_run_context_can_override_compatibility_profile():
    pipeline = RecordingRuntime()
    context_profile = SimpleNamespace(name="override", tool_mode="bot")

    pipeline.run(
        BOT_AGENT_SPEC,
        "hello",
        RunContext(
            session=Session(id="phase2:override"),
            profile=context_profile,
        ),
    )

    assert pipeline.calls[0]["profile"] is context_profile
