from __future__ import annotations

import json
from pathlib import Path

from applications.coding.runner import CodingApplication
from applications.coding.session import TaskSessionFactory
from models.provider import LLMResponse, ToolCall
from runtime.context import ContextBuilder
from runtime.runtime import RunContext, Runtime
from runtime.sessions import Session
from runtime.trace.run_state import RunState
from tools.executor import ToolExecutor
from tools.schema import function_tool
from tools.spec import ToolExposure, ToolSpec
from tools.tool_registry import ToolRegistry
from tests.fakes import make_agent_spec
from tests.fakes.in_memory_sessions import InMemorySessionManager


class _StreamingProvider:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.responses = [
            _tool_response("先检查 registry。", "call-1"),
            _tool_response("发现 policy 分离，继续检查。", "call-2"),
            LLMResponse(
                content="最终结论。",
                raw_message={"role": "assistant", "content": "最终结论。"},
            ),
        ]
        self.streaming_calls = []

    def stream_chat(self, *, on_text, **kwargs):
        self.streaming_calls.append(kwargs)
        response = self.responses.pop(0)
        for chunk in _chunks(response.content or ""):
            self.events.append(f"delta:{chunk}")
            on_text(chunk)
        return response

    def chat(self, **kwargs):
        return LLMResponse(
            content=json.dumps({"summary": "done", "conclusions": []}),
            raw_message={"role": "assistant", "content": "done"},
        )


class _TraceStore:
    def __init__(self, events: list[str], root: Path) -> None:
        self.events = events
        self.root = root

    def append_event(self, run_state, event, payload, **kwargs):
        if event.startswith("assistant.") or event.startswith("tool.call."):
            self.events.append(event)

    def write_run_state(self, run_state):
        return None

    def run_dir(self, run_state):
        path = self.root / "run"
        path.mkdir(parents=True, exist_ok=True)
        return path


def _tool_response(content: str, call_id: str) -> LLMResponse:
    call = ToolCall(id=call_id, name="read_file", arguments={})
    return LLMResponse(
        content=content,
        tool_calls=[call],
        raw_message={
            "role": "assistant",
            "content": content,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
    )


def _chunks(content: str) -> list[str]:
    midpoint = max(1, len(content) // 2)
    return [content[:midpoint], content[midpoint:]] if content[midpoint:] else [content]


def test_coding_stream_preserves_intermediate_text_tool_order_and_completion(tmp_path):
    events: list[str] = []
    registry = ToolRegistry()

    def read_file():
        events.append("tool:read_file")
        return "file contents"

    registry.register(ToolSpec(
        schema=function_tool("read_file", "Read one file.", {}, []),
        handler=read_file,
        allowed_modes=frozenset({"coding"}),
        exposure=ToolExposure.PRELOADED,
    ))
    provider = _StreamingProvider(events)
    sessions = InMemorySessionManager()
    runtime = Runtime(
        tools=registry,
        provider=provider,
        model="scripted-coding",
        tool_executor=ToolExecutor([]),
        context_builder=ContextBuilder(),
        max_reasoning_steps=6,
    )
    coding = CodingApplication(
        sessions=sessions,
        base_runtime=runtime,
        workspace_root=tmp_path,
    )
    coding.factory = TaskSessionFactory(sessions, root=tmp_path / "tasks")
    parent = Session(id="web:local:stream", active_agent="coding")
    run_state = RunState.create(run_id="stream-run", session_id=parent.id)
    trace = _TraceStore(events, tmp_path)
    visible: list[str] = []

    def on_text(text: str) -> None:
        visible.append(text)

    setattr(
        on_text,
        "on_assistant_segment",
        lambda payload: events.append(
            "segment:final" if payload["final"] else "segment:progress"
        ),
    )
    setattr(
        on_text,
        "on_assistant_completed",
        lambda payload: events.append("frontend:assistant_completed"),
    )

    reply = coding.run_coding_task(
        parent_session=parent,
        user_text="inspect the runtime",
        agent_spec=make_agent_spec("coding", "coding", "coding"),
        on_text=on_text,
        run_state=run_state,
        trace_store=trace,
    )

    assert len(provider.streaming_calls) == 3
    assert "最终结论。" in reply
    assert "".join(visible) == "先检查 registry。发现 policy 分离，继续检查。最终结论。"
    semantic_events = [
        "delta" if event.startswith("delta:") else event
        for event in events
        if event.startswith("delta:")
        or event.startswith("segment:")
        or event in {"tool.call.started", "tool.call.completed", "frontend:assistant_completed"}
    ]
    assert semantic_events == [
        "delta", "delta", "segment:progress",
        "tool.call.started", "tool.call.completed",
        "delta", "delta", "segment:progress",
        "tool.call.started", "tool.call.completed",
        "delta", "delta", "segment:final",
        "frontend:assistant_completed",
    ]
    assert events.index("segment:progress") < events.index("tool.call.started")
    first_tool = events.index("tool:read_file")
    second_progress = events.index("segment:progress", events.index("segment:progress") + 1)
    second_tool = events.index("tool:read_file", first_tool + 1)
    final_segment = events.index("segment:final")
    completed = events.index("frontend:assistant_completed")
    assert first_tool < second_progress < second_tool < final_segment < completed
    assert events.count("frontend:assistant_completed") == 1


def test_cancelled_stream_keeps_visible_text_and_emits_terminal_fact(tmp_path):
    events: list[str] = []
    registry = ToolRegistry()

    def read_file():
        events.append("tool:read_file")
        return "file contents"

    registry.register(ToolSpec(
        schema=function_tool("read_file", "Read one file.", {}, []),
        handler=read_file,
        allowed_modes=frozenset({"coding"}),
        exposure=ToolExposure.PRELOADED,
    ))
    provider = _StreamingProvider(events)
    provider.responses = [_tool_response("已完成第一步。", "call-1")]
    sessions = InMemorySessionManager()
    runtime = Runtime(
        tools=registry,
        provider=provider,
        model="scripted-coding",
        tool_executor=ToolExecutor([]),
        context_builder=ContextBuilder(),
        max_reasoning_steps=6,
    )
    session = Session(id="coding:cancelled", active_agent="coding")
    session.add_message("user", "inspect the runtime")
    run_state = RunState.create(run_id="cancel-run", session_id=session.id)
    trace = _TraceStore(events, tmp_path)
    callback_events: list[str] = []

    def on_text(text: str) -> None:
        callback_events.append(text)

    setattr(
        on_text,
        "on_assistant_completed",
        lambda payload: callback_events.append(f"completed:{payload['reason']}"),
    )

    runtime.run(
        make_agent_spec("coding", "coding", "coding"),
        "inspect the runtime",
        context=RunContext(
            session=session,
            on_text=on_text,
            cancel_requested=lambda: bool(events.count("tool:read_file")),
            run_state=run_state,
            trace_store=trace,
        ),
    )

    assert "".join(callback_events[:2]) == "已完成第一步。"
    assert callback_events[-1] == "completed:user_cancelled"
    assert "已完成第一步。" in "".join(callback_events)
    assert run_state.status == "stopped"
    assert run_state.stop_reason == "user_cancelled"
    assert len(provider.streaming_calls) == 1
