from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.fakes import make_agent_spec
from runtime.context import ContextBundle
from runtime.execution.failure_reasons import REASONING_LOOP_STOP_REASON_KEY, StopReason
from runtime.execution.reasoning_loop import ReasoningLoop
from runtime.sessions import Session
from tools.executor import ToolExecutor
from tests.fakes.fake_tools import RecordingTool, registry_with_tool
from tests.fakes.scripted_model import (
    FinalResponse,
    ModelFailure,
    ScriptedModel,
    ToolResponse,
)


PROFILE = make_agent_spec(name="baseline", system_prompt="baseline prompt", tool_mode="bot")


def _run(model, registry, session=None, **kwargs):
    session = session or Session(id="phase0:loop")
    loop = ReasoningLoop(
        tools=registry,
        tool_executor=ToolExecutor([]),
        max_tokens=256,
        max_reasoning_steps=kwargs.pop("max_reasoning_steps", 8),
    )
    loop.run(
        session=session,
        profile=PROFILE,
        build_context=lambda current, profile, **ignored: ContextBundle(messages=[
            {"role": "system", "content": profile.system_prompt},
            *current.messages,
        ]),
        resolve_provider=lambda current, profile: (model, "fake-model"),
        after_turn=lambda current: current.touch(),
        **kwargs,
    )
    return session


def test_direct_final_response_records_model_input_and_assistant_message():
    model = ScriptedModel([FinalResponse("hello")])
    registry = registry_with_tool("example_tool", RecordingTool(), modes={"bot"})
    session = Session(id="phase0:direct")
    session.add_message("user", "hi")

    _run(model, registry, session)

    assert [item["role"] for item in session.messages] == ["user", "assistant"]
    assert session.messages[-1]["content"] == "hello"
    assert model.calls[0]["model"] == "fake-model"
    assert model.calls[0]["messages"] == [
        {"role": "system", "content": "baseline prompt"},
        session.messages[0],
    ]
    assert model.calls[0]["tools"][0]["function"]["name"] == "example_tool"


def test_tool_call_result_is_correlated_and_returned_to_next_model_round():
    tool = RecordingTool(output="value=2")
    model = ScriptedModel([
        ToolResponse("example_tool", {"value": 1}, call_id="call-tool"),
        FinalResponse("done"),
    ])
    session = Session(id="phase0:tool")
    session.add_message("user", "use the tool")

    _run(model, registry_with_tool("example_tool", tool, modes={"bot"}), session)

    assert len(model.calls) == 2
    assert tool.calls[0]["value"] == 1
    assert tool.calls[0]["_trace_store"] is None
    assert tool.calls[0]["_run_state"] is None
    assert tool.calls[0]["_parent_span_id"].endswith(":tool:1:call-tool")
    tool_messages = [item for item in session.messages if item["role"] == "tool"]
    assert tool_messages == [{
        "role": "tool",
        "tool_call_id": "call-tool",
        "content": "value=2",
        "status": "success",
        "final_arguments": {"value": 1},
        "metadata": {},
        "pre_hook_trace": [],
        "post_hook_trace": [],
    }]
    assert any(
        item["role"] == "tool" and item["tool_call_id"] == "call-tool"
        for item in model.calls[1]["messages"]
    )
    assert session.messages[-1]["content"] == "done"


def test_tool_handler_exception_is_normalized_and_model_can_recover():
    tool = RecordingTool(error=ValueError("broken"))
    model = ScriptedModel([
        ToolResponse("example_tool", {"value": 1}, call_id="call-error"),
        FinalResponse("recovered"),
    ])

    session = _run(model, registry_with_tool("example_tool", tool, modes={"bot"}))

    tool_message = next(item for item in session.messages if item["role"] == "tool")
    # ToolRegistry currently converts handler exceptions to a string before ToolExecutor.
    assert tool_message["content"] == "Error: broken"
    assert tool_message["status"] == "success"
    assert session.messages[-1]["content"] == "recovered"


def test_cancellation_before_first_step_stops_without_calling_model():
    model = ScriptedModel([FinalResponse("must not run")])
    session = _run(
        model,
        registry_with_tool("example_tool", RecordingTool(), modes={"bot"}),
        cancel_requested=lambda: True,
    )

    assert model.calls == []
    assert session.metadata[REASONING_LOOP_STOP_REASON_KEY] == StopReason.USER_CANCELLED.value
    assert session.messages[-1]["role"] == "assistant"


def test_step_limit_preserves_current_stop_contract():
    model = ScriptedModel([
        ToolResponse("example_tool", {"value": 1}, call_id="one"),
        ToolResponse("example_tool", {"value": 2}, call_id="two"),
    ])
    session = _run(
        model,
        registry_with_tool("example_tool", RecordingTool(), modes={"bot"}),
        max_reasoning_steps=1,
    )

    assert len(model.calls) == 1
    assert session.metadata[REASONING_LOOP_STOP_REASON_KEY] == (
        StopReason.REASONING_STEP_LIMIT.value
    )
    assert "超过上限" in session.messages[-1]["content"]


def test_model_exception_propagates_unchanged():
    model = ScriptedModel([ModelFailure(TimeoutError("model timeout"))])

    with pytest.raises(TimeoutError, match="model timeout"):
        _run(
            model,
            registry_with_tool("example_tool", RecordingTool(), modes={"bot"}),
        )


def test_streaming_chunks_and_final_session_message_have_same_text():
    model = ScriptedModel(
        [FinalResponse("你好")],
        stream_chunks=["你", "好"],
    )
    chunks = []

    session = _run(
        model,
        registry_with_tool("example_tool", RecordingTool(), modes={"bot"}),
        on_text=chunks.append,
    )

    assert chunks == ["你", "好"]
    assert session.messages[-1]["content"] == "".join(chunks)
    assert len(model.streaming_calls) == 1
