from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from applications.coding.session import TaskSessionFactory
from agents.subagent.runner import TaskSubagentRunner
from tests.fakes import make_agent_spec
from runtime.context import (
    ContextBuilder,
    ContextBundle,
    ContextMemoryService,
    PromptAssetsService,
)
from runtime.context.budget import ContextBudgeter
from runtime.context.providers import DEFAULT_CONTEXT_PROVIDERS
from runtime.execution.failure_reasons import REASONING_LOOP_STOP_REASON_KEY, StopReason
from runtime.runtime import Runtime
from runtime.trace.run_state import RunState
from runtime.sessions import Session
from tests.fakes.fake_tools import RecordingTool, registry_with_tool
from tests.fakes.scripted_model import FinalResponse, ScriptedModel, ToolResponse
from tools.executor import ToolExecutor


SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


class RecordingTrace:
    def __init__(self):
        self.events = []
        self.states = []

    def append_event(self, run_state, event_name, payload, **kwargs):
        self.events.append({
            "event": event_name,
            "step": kwargs.get("step"),
            "span": bool(kwargs.get("span_id")),
            "parent": bool(kwargs.get("parent_span_id")),
        })

    def write_run_state(self, run_state):
        self.states.append(run_state.to_dict())


class MemoryStore:
    def __init__(self, text):
        self.text = text

    def read_all(self):
        return self.text


class InMemorySessions:
    def __init__(self):
        self.sessions = {}
        self.saved = []

    def get_or_create(self, session_id):
        self.sessions.setdefault(session_id, Session(id=session_id))
        return self.sessions[session_id]

    def save(self, session):
        self.saved.append(session.id)


def _tiny_context(session, profile, **kwargs):
    return ContextBundle(messages=[
        {"role": "system", "content": profile.system_prompt},
        *session.messages,
    ])


def test_trace_event_order_for_model_tool_model_run_matches_snapshot():
    provider = ScriptedModel([
        ToolResponse("example_tool", {"value": 1}, "trace-tool"),
        FinalResponse("done"),
    ])
    trace = RecordingTrace()
    run_state = RunState.create(
        run_id="run-phase0",
        session_id="phase0:trace",
        mode="bot",
    )
    pipeline = Runtime(
        tools=registry_with_tool(
            "example_tool",
            RecordingTool(output="tool-output"),
            modes={"bot"},
        ),
        provider=provider,
        model="fake-model",
        tool_executor=ToolExecutor([]),
        context_builder=SimpleNamespace(build=_tiny_context),
        max_tokens=256,
    )
    session = Session(id="phase0:trace", active_agent="bot")
    session.add_message("user", "trace it")

    pipeline.run_turn(
        session,
        make_agent_spec("chat", "trace prompt", "bot"),
        run_state=run_state,
        trace_store=trace,
    )

    actual = [item["event"] for item in trace.events]
    expected = json.loads(
        (SNAPSHOT_DIR / "runtime_phase0_trace_events.json").read_text(encoding="utf-8")
    )
    assert actual == expected
    assert run_state.reasoning_steps == 2
    assert run_state.tool_calls == 1


def test_cancellation_requested_during_tool_is_observed_after_tool_completes():
    cancellation = {"requested": False}

    def complete_then_cancel(**kwargs):
        cancellation["requested"] = True
        return "tool completed before cancellation was observed"

    provider = ScriptedModel([
        ToolResponse("example_tool", {"value": 1}, "cancel-tool"),
        FinalResponse("must not be requested"),
    ])
    pipeline = Runtime(
        tools=registry_with_tool(
            "example_tool",
            complete_then_cancel,
            modes={"bot"},
        ),
        provider=provider,
        model="fake-model",
        tool_executor=ToolExecutor([]),
        context_builder=SimpleNamespace(build=_tiny_context),
        max_tokens=256,
    )
    session = Session(id="phase0:cancel-during-tool", active_agent="bot")
    session.add_message("user", "run then cancel")

    reply = pipeline.run_turn(
        session,
        make_agent_spec("chat", "cancel prompt", "bot"),
        cancel_requested=lambda: cancellation["requested"],
    )

    assert len(provider.calls) == 1
    assert any(
        message.get("role") == "tool"
        and "tool completed" in message.get("content", "")
        for message in session.messages
    )
    assert session.metadata[REASONING_LOOP_STOP_REASON_KEY] == StopReason.USER_CANCELLED.value
    assert "用户请求停止" in reply


def test_memory_disabled_and_enabled_context_injection_position(tmp_path):
    session = Session(id="phase0:memory")
    session.add_message("user", "remember my preference")
    profile = make_agent_spec("chat", "base prompt", "bot")
    budgeter = ContextBudgeter.from_env()
    common = {
        "budgeter": budgeter,
        "context_providers": DEFAULT_CONTEXT_PROVIDERS,
        "prompt_assets_service": PromptAssetsService(
            budgeter=budgeter,
            instruction_root=tmp_path,
            skill_loader=SimpleNamespace(catalog_text=lambda: ""),
        ),
    }

    disabled = ContextBuilder(
        memory_service=ContextMemoryService(),
        **common,
    ).build(
        session=session,
        profile=profile,
    )
    enabled = ContextBuilder(
        memory_service=ContextMemoryService(
            memory_store=MemoryStore("Use pytest fixtures."),
        ),
        **common,
    ).build(session=session, profile=profile)

    disabled_report = disabled.report.to_dict()["sections"]["memory"]
    enabled_report = enabled.report.to_dict()["sections"]["memory"]
    assert disabled_report["rendered_chars"] == 0
    assert enabled_report["rendered_chars"] > 0
    assert enabled.messages[1]["role"] == "user"
    assert "Use pytest fixtures." in enabled.messages[1]["content"]
    assert enabled.messages[-1]["content"] == "remember my preference"


def test_coding_application_factory_isolates_metadata_and_memory_root(tmp_path):
    sessions = InMemorySessions()
    parent = Session(
        id="web:parent",
        metadata={"user_id": "u1", "workspace_root": "/parent/workspace"},
    )
    factory = TaskSessionFactory(sessions, root=tmp_path / "task-sessions")

    record = factory.create(
        parent_session_id=parent.id,
        task_type="coding",
        user_request="fix it",
        user_id="u1",
        user_role="admin",
    )

    assert record.session is not parent
    assert record.session.id.startswith("task:coding-")
    assert record.session.active_agent == "coding"
    assert record.session.metadata["parent_session_id"] == parent.id
    assert record.session.metadata["status"] == "running"
    assert record.session.metadata["user_id"] == "u1"
    assert "workspace_root" not in record.session.metadata
    assert record.memory_root.exists()
    assert parent.metadata == {"user_id": "u1", "workspace_root": "/parent/workspace"}


def test_subagent_uses_independent_session_filtered_tools_and_structured_result():
    answer = json.dumps({
        "schema_version": "subagent.explore.v1",
        "agent_type": "explore",
        "status": "completed",
        "summary": "inspected",
        "payload": {
            "findings": [],
            "evidence": [],
            "covered_scope": [],
            "open_questions": [],
            "needs_parent_verification": False,
        },
        "incomplete": False,
        "failure_reason": None,
        "failure_message": None,
        "recoverable": False,
        "retry_hint": None,
    })
    provider = ScriptedModel([FinalResponse(answer)])
    base_pipeline = Runtime(
        tools=registry_with_tool(
            "read_file",
            RecordingTool(output="content"),
            modes={"coding"},
        ),
        provider=provider,
        model="fake-model",
        tool_executor=ToolExecutor([]),
        context_builder=SimpleNamespace(build=_tiny_context),
        max_tokens=256,
    )
    runner = TaskSubagentRunner(base_pipeline=base_pipeline, max_reasoning_steps=4)
    parent = Session(
        id="web:parent",
        metadata={
            "user_id": "u1",
            "user_role": "admin",
            "workspace_root": "/tmp/phase0",
        },
    )
    parent.add_message("user", "parent history must not be copied verbatim")

    result = runner.run(
        prompt="inspect one file",
        agent_type="explore",
        parent_session=parent,
    )

    assert result.success
    assert result.summary == "inspected"
    assert len(provider.calls) == 1
    model_messages = provider.calls[0]["messages"]
    assert all("parent history must not be copied verbatim" not in str(item) for item in model_messages)
    assert provider.calls[0]["tools"][0]["function"]["name"] == "read_file"
    assert parent.messages == [{
        "role": "user",
        "content": "parent history must not be copied verbatim",
        "timestamp": parent.messages[0]["timestamp"],
    }]
