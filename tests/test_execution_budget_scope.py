from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
from time import monotonic

import pytest

from models.provider import LLMResponse, ToolCall, LLMUsage
from runtime.execution.budget import (ExecutionBudget, ExecutionBudgetExceeded,
    ExecutionCancelled, execution_scope, current_budget)
from runtime.runtime import RunContext
from runtime.sessions import Session
from runtime.workspace_lease import workspace_writer, WorkspaceBusyError
from runtime.trace.trace_store import TraceStore
from runtime.trace.run_state import RunState
from runtime.task_state import ensure_task_state_core, load_task_state_core, TaskStateRunObserver
from runtime.execution.state import RunExecutionState
from runtime.execution.failure_reasons import StopDecision, StopReason
from tests.test_run_trace import _runtime, ScriptedProvider, _tool_response, _final_response
from tests.fakes import make_agent_spec


def test_nested_and_thread_scopes_share_limits_without_reset():
    budget = ExecutionBudget(max_model_attempts=2)
    with execution_scope(budget=budget):
        with execution_scope() as nested:
            assert nested is budget
            nested.claim("model")
        with ThreadPoolExecutor(1) as pool:
            pool.submit(copy_context().run, lambda: current_budget().claim("model")).result()
        with pytest.raises(ExecutionBudgetExceeded):
            budget.claim("model")
    assert current_budget() is None
    assert budget.model_attempts == 2


def test_cancellation_error_is_fail_closed_and_usage_unknown_is_not_zero():
    def broken(): raise RuntimeError("missing cancellation backend")
    budget = ExecutionBudget(cancel_requested=broken)
    with pytest.raises(ExecutionCancelled): budget.claim("tool")
    assert budget.tool_attempts == 0
    budget.record_usage(None)
    budget.record_usage(LLMUsage(input_tokens=3, output_tokens=2))
    assert budget.snapshot()["known_tokens"] == 5
    assert budget.snapshot()["unknown_usage_attempts"] == 1
    assert not budget.snapshot()["usage_complete"]


def test_expired_budget_stops_before_provider():
    provider = ScriptedProvider([_final_response()])
    runtime = _runtime(provider)
    session = Session("budget:deadline")
    session.add_message("user", "do work")
    with execution_scope(budget=ExecutionBudget(deadline=monotonic()-1)):
        result = runtime.run(make_agent_spec("chat", "", "bot"), "do work", RunContext(session=session))
    assert result.execution.stop_reason == "hard_budget_exceeded"
    assert not provider.calls


def test_cancel_between_tool_calls_closes_all_calls_without_second_effect():
    first = _tool_response(1)
    second = _tool_response(2)
    first.tool_calls.extend(second.tool_calls)
    first.raw_message["tool_calls"].extend(second.raw_message["tool_calls"])
    provider = ScriptedProvider([first, _final_response("must not be called")])
    runtime = _runtime(provider)
    invoked = []
    # Callback changes when the host tool completes, not on an arbitrary poll count.
    runtime.agent_runner.tools._tools["echo"].handler = lambda **kwargs: invoked.append(kwargs) or "ok"
    session = Session("cancel:batch")
    session.add_message("user", "run both")
    context = RunContext(session=session, cancel_requested=lambda: bool(invoked))
    result = runtime.run(make_agent_spec("chat", "", "bot"), "run both", context)
    assert len(invoked) == 1
    assert len(provider.calls) == 1
    assert result.execution.stop_reason == "user_cancelled"
    tool_messages = [m for m in session.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["call-1", "call-2"]
    assert tool_messages[1]["status"] == "cancelled"
    assert tool_messages[1]["metadata"]["effect_state"] == "not_started"


def test_same_canonical_workspace_has_one_writer(tmp_path):
    alias = tmp_path / "alias"
    root = tmp_path / "repo"
    root.mkdir()
    alias.symlink_to(root, target_is_directory=True)
    with workspace_writer(root):
        with pytest.raises(WorkspaceBusyError):
            with workspace_writer(alias): pass
    with workspace_writer(alias): pass


def test_coding_final_text_does_not_complete_unverified_task():
    session = Session("coding:outcome", metadata={"kind": "coding_application"})
    ensure_task_state_core(session, objective="fix behavior")
    execution = RunExecutionState(stop_decision=StopDecision(StopReason.COMPLETED, "done"))
    TaskStateRunObserver().after_run(session=session, execution=execution)
    assert str(load_task_state_core(session).status) == "active"
    assert execution.stop_reason == "completed"


def test_trace_default_omits_text_and_preserves_unknown_usage(tmp_path):
    trace = TraceStore(tmp_path, index_enabled=False)
    state = RunState.create(session_id="trace:privacy", metadata={"private": "SECRET"})
    trace.start_run(state)
    trace.append_event(state, "model.call.completed", {"model": "fake", "usage": {}, "output_preview": "SECRET"})
    trace.append_event(state, "model.call.completed", {"model": "fake", "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}})
    state.finish_success("SECRET")
    trace.write_run_state(state)
    trace.write_report(state, {"reply": "SECRET"})
    for path in trace.run_dir(state).glob("*.json*"):
        assert "SECRET" not in path.read_text()
    events = [json.loads(line) for line in (trace.run_dir(state)/"trace.jsonl").read_text().splitlines()]
    assert len({e["event_id"] for e in events}) == len(events)
    metrics = json.loads((trace.run_dir(state)/"metrics.json").read_text())
    assert metrics["total_tokens"] is None
    assert metrics["known_total_tokens"] == 3
    assert metrics["usage_status"] == "partial"
    assert metrics["unknown_usage_calls"] == 1


def test_trace_content_requires_explicit_opt_in(tmp_path):
    trace = TraceStore(tmp_path, index_enabled=False, capture_content=True)
    state = RunState.create(session_id="debug")
    trace.append_event(state, "debug", {"text": "opt-in diagnostic"})
    assert "opt-in diagnostic" in (trace.run_dir(state)/"trace.jsonl").read_text()


def test_parent_child_workspaces_cannot_write_concurrently(tmp_path):
    child = tmp_path / "nested"
    child.mkdir()
    with workspace_writer(tmp_path):
        with pytest.raises(WorkspaceBusyError):
            with workspace_writer(child): pass
    with workspace_writer(child): pass


def test_proposed_memory_renders_as_pending_without_claiming_promotion():
    from applications.coding.runner import CodingApplication
    from applications.coding.promotion import PromotionResult
    from applications.coding.conclusions import ConclusionCandidate
    from types import SimpleNamespace
    application = object.__new__(CodingApplication)
    record = SimpleNamespace(task_id="task-test", session=Session("task", metadata={"status":"awaiting_verification"}))
    promotion = PromotionResult(proposed=[ConclusionCandidate(category="project", content="candidate")], proposal_ids=["proposal-1"])
    text = application._format_parent_reply(record,"generated",promotion,None)
    assert "pending human review and confirmation" in text
    assert "Submitted" not in text
    assert "awaiting_verification" in text


def test_timed_out_worker_cannot_claim_after_parent_scope_finishes():
    from agents.subagent.parallel import run_parallel_tasks
    from threading import Event
    from types import SimpleNamespace
    release, finished = Event(), Event()
    errors = []
    class LateWorker:
        def run(self, **kwargs):
            release.wait(1)
            try: current_budget().claim("model")
            except (ExecutionCancelled, ExecutionBudgetExceeded) as exc: errors.append(exc)
            finally: finished.set()
            return {"success": False}
    budget = ExecutionBudget()
    with execution_scope(budget=budget):
        result = run_parallel_tasks(runner=LateWorker(), tasks=[{"agent_type":"explore"}], timeout_seconds=0.01)
        assert result[0]["failure_reason"] == "subagent_timeout"
    release.set()
    assert finished.wait(1)
    assert errors
    assert budget.model_attempts == 0


def test_inflight_usage_is_incomplete_at_root_finalization():
    budget = ExecutionBudget()
    with execution_scope(budget=budget):
        budget.claim("model")
    final = budget.snapshot()
    assert final["closed"]
    assert not final["usage_complete"]
    assert final["model_attempts_in_flight"] == 1
    with pytest.raises(ExecutionCancelled): budget.claim("model")


def test_standalone_coding_budget_includes_failed_and_auxiliary_attempts(tmp_path):
    from applications.coding.runner import CodingApplication
    from runtime.workspace import WorkspaceResolver
    application = object.__new__(CodingApplication)
    application.workspace_resolver = WorkspaceResolver(allowed_roots=[tmp_path], default_workspace=tmp_path)
    def execute(**kwargs):
        budget = current_budget()
        budget.claim("model"); budget.record_usage(None)
        budget.claim("model"); budget.record_usage(LLMUsage(2,1,3))
        return "generated"
    application._run_coding_task = execute
    run = RunState.create(session_id="standalone")
    trace = TraceStore(tmp_path/"trace",index_enabled=False)
    assert application.run_coding_task(parent_session=Session("parent"),user_text="task",agent_spec=None,
        run_state=run,trace_store=trace)=="generated"
    final = run.metadata["execution_budget"]
    assert final["closed"] and final["model_attempts"] == 2
    metrics = trace._metrics_for(run)
    assert metrics["total_tokens"] is None and metrics["known_total_tokens"] == 3
    assert metrics["unknown_usage_calls"] == 1
    assert metrics["usage_scope"] == "root_all_purposes"


def test_nested_coding_does_not_close_parent_budget(tmp_path):
    from applications.coding.runner import CodingApplication
    from runtime.workspace import WorkspaceResolver
    application = object.__new__(CodingApplication)
    application.workspace_resolver = WorkspaceResolver(allowed_roots=[tmp_path],default_workspace=tmp_path)
    application._run_coding_task = lambda **kwargs: "done"
    with execution_scope() as budget:
        application.run_coding_task(parent_session=Session("parent"),user_text="task",agent_spec=None)
        assert not budget.closed
        budget.claim("model")


def test_turn_releases_cancellation_scope_when_final_trace_write_fails(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from applications.turn_coordinator import TurnCoordinator
    from runtime.cancellation import CancellationRegistry

    session = Session("turn:trace-failure")
    state = RunState.create(session_id=session.id)
    registry = CancellationRegistry()
    observed = []

    class FailingTrace:
        def append_event(self, *args):
            raise OSError("diagnostic disk unavailable")

    coordinator = TurnCoordinator(
        bus=None, sessions=SimpleNamespace(get_or_create=lambda key: session),
        runtime=None, router=None, trace_store=FailingTrace(),
        cancellation_registry=registry,
    )
    monkeypatch.setattr(coordinator, "_externalize_inbound_artifacts", lambda inbound: [])
    monkeypatch.setattr(coordinator, "_receive", lambda *args: state)

    async def handled(*args):
        observed.append(current_budget())
        assert registry.token(coordinator._turn_scope(session.id)) is not None
        return True

    monkeypatch.setattr(coordinator, "_preprocess", handled)
    with pytest.raises(OSError, match="diagnostic disk unavailable"):
        asyncio.run(coordinator.run_inbound(SimpleNamespace(session_key=session.id)))
    assert registry.token(coordinator._turn_scope(session.id)) is None
    assert observed[0].closed
    assert current_budget() is None
