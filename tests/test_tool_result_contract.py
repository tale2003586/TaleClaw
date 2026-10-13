from types import SimpleNamespace

import pytest

from runtime.execution.budget import ExecutionBudget, execution_scope
from tools.executor import ToolExecutionRequest, ToolExecutor, ToolHandlerOutput
from tools.hooks import ShellSafetyHook, ToolLoopGuardHook, ToolTraceHook
from tools.schema import function_tool
from tools.spec import ToolSpec
from tools.tool_registry import ToolRegistry, build_lead_tool_registry


def request(name="example", arguments=None, session=None):
    return ToolExecutionRequest(
        call_id="call-1", tool_name=name, arguments=arguments if arguments is not None else {},
        session_id=getattr(session, "id", "test"),
        metadata=getattr(session, "metadata", {}), session=session,
    )


def registry_for(handler, *, properties=None, required=None, side_effect=False):
    registry = ToolRegistry()
    registry.register(ToolSpec(
        schema=function_tool("example", "example", properties or {}, required or []),
        handler=handler, side_effect=side_effect,
    ))
    return registry


def execute(registry, req=None, executor=None):
    req = req or request()
    return (executor or ToolExecutor()).execute(
        req, lambda name, args: registry.execute(name, args, session=req.session),
    )


def test_explicit_result_is_string_compatible_and_keeps_facts_in_trace():
    output = ToolHandlerOutput("(no output)", status="error", exit_code=9,
                               error_type="CommandExitError", effect_state="unknown")
    trace = ToolTraceHook()
    result = execute(registry_for(lambda: output, side_effect=True), executor=ToolExecutor([trace]))
    assert isinstance(output, str)
    assert output == "(no output)"
    assert result.status == "error" and result.exit_code == 9
    assert result.metadata["exit_code"] == 9
    assert result.metadata["effect_state"] == result.effect_state == "unknown"
    assert trace.records[-1]["status"] == "error"
    assert trace.records[-1]["exit_code"] == 9


def test_error_text_in_explicit_success_is_data_not_status():
    result = execute(registry_for(lambda: ToolHandlerOutput("Error: quoted log example", effect_state="no_effect")))
    assert result.status == "success"
    assert result.effect_state == "no_effect"


@pytest.mark.parametrize("handler", [lambda: "Error: legacy failure", lambda: 1 / 0])
def test_legacy_errors_and_handler_exceptions_are_not_success(handler):
    result = execute(registry_for(handler, side_effect=True))
    assert result.status == "error"
    assert result.error_type
    assert result.effect_state == "unknown"
    assert result.metadata["status"] == "error"


@pytest.mark.parametrize("arguments", [
    {}, {"count": True}, {"count": "2"}, {"count": 0}, {"count": 4},
    {"count": 1, "unexpected": 2}, {"count": 1, "_session": {}},
    {"count": 1, "records": [{}]}, {"count": 1, "records": []},
    {"count": 1, "records": [{"path": 9}]},
])
def test_schema_invalid_input_does_not_invoke_handler(arguments):
    calls = []
    registry = registry_for(lambda **args: calls.append(args), properties={
        "count": {"type": "integer", "minimum": 1, "maximum": 3},
        "records": {"type": "array", "minItems": 1, "items": {
            "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
        }},
    }, required=["count"])
    result = execute(registry, request(arguments=arguments))
    assert result.status == "error"
    assert result.error_type == "InvalidToolArguments"
    assert result.effect_state == "not_started"
    assert not calls


def test_mode_specific_schema_is_used_and_runtime_values_not_taken_from_input():
    calls = []
    registry = registry_for(lambda **args: calls.append(args) or "ok", properties={"coding": {"type": "string"}})
    spec = registry.spec_for("example")
    spec.schemas_by_mode["bot"] = function_tool("example", "bot", {"bot": {"type": "string"}}, ["bot"])
    spec.session_scoped = True
    session = SimpleNamespace(metadata={})
    denied = registry.execute("example", {"coding": "x"}, session=session, mode="bot")
    assert denied.status == "error" and not calls
    accepted = registry.execute("example", {"bot": "x"}, session=session, mode="bot")
    assert accepted.status == "success"
    assert calls == [{"bot": "x", "_session": session}]


def test_nonobject_and_unknown_tool_fail_before_handler():
    registry = registry_for(lambda: pytest.fail("must not execute"))
    result = execute(registry, request(arguments=[1]))
    assert result.status == "error" and result.effect_state == "not_started"
    result = execute(registry, request(name="missing"))
    assert result.status == "denied" and result.effect_state == "not_started"


def test_registry_owned_tool_search_validates_before_unlocking():
    session = SimpleNamespace(id="s", metadata={})
    registry = build_lead_tool_registry()
    result = registry.execute("tool_search", {"query": ["bash"]}, session=session)
    assert result.status == "error"
    assert not session.metadata.get("unlocked_tools")


def test_declared_unsupported_constraint_fails_closed():
    registry = registry_for(lambda **args: pytest.fail("must not execute"),
                            properties={"x": {"type": "string", "$ref": "remote"}})
    result = execute(registry, request(arguments={"x": "unsafe"}))
    assert result.status == "error"
    assert "unsupported" in result.output


def test_pre_denial_and_budget_failure_do_not_claim_an_effect():
    calls = []
    budget = ExecutionBudget(max_tool_attempts=0)
    with execution_scope(budget=budget):
        result = ToolExecutor().execute(request(), lambda *args: calls.append(args))
    assert result.status == "timeout" and result.effect_state == "not_started"
    assert not calls and budget.tool_attempts == 0
    result = ToolExecutor([ShellSafetyHook()]).execute(
        request("bash", {"command": "rm -rf /"}), lambda *args: calls.append(args),
    )
    assert result.status == "denied" and result.metadata["effect_state"] == "not_started"
    assert not calls


def test_cached_read_does_not_consume_another_tool_attempt():
    calls = []
    budget = ExecutionBudget(max_tool_attempts=1)
    executor = ToolExecutor([ToolLoopGuardHook()])
    with execution_scope(budget=budget):
        first = executor.execute(request("read_file", {"path": "x"}), lambda *args: calls.append(args) or "contents")
        second = executor.execute(request("read_file", {"path": "x"}), lambda *args: calls.append(args) or "wrong")
    assert first.status == second.status == "success"
    assert len(calls) == budget.tool_attempts == 1
    assert second.effect_state == "no_effect"


@pytest.mark.parametrize("mutation", ["write_file", "bash", "failing_plugin"])
def test_mutation_invalidates_hook_and_session_read_cache(tmp_path, mutation):
    (tmp_path / "a.txt").write_text("before")
    session = SimpleNamespace(id="task:a", metadata={"workspace_root": str(tmp_path)})
    registry = build_lead_tool_registry()
    if mutation == "failing_plugin":
        def mutate():
            (tmp_path / "a.txt").write_text("after")
            raise RuntimeError("after-effect failure")
        registry.register(ToolSpec(schema=function_tool(mutation, "mutate", {}), handler=mutate, side_effect=True))
    executor = ToolExecutor([ToolLoopGuardHook()])
    before = execute(registry, request("read_file", {"path": "a.txt"}, session), executor)
    assert "before" in before.output
    args = {"path": "a.txt", "content": "after"} if mutation == "write_file" else (
        {"command": "printf after > a.txt"} if mutation == "bash" else {})
    changed = execute(registry, request(mutation, args, session), executor)
    assert changed.status == ("error" if mutation == "failing_plugin" else "success")
    after = execute(registry, request("read_file", {"path": "a.txt"}, session), executor)
    assert "after" in after.output and "before" not in after.output
    assert "replayed from cache" not in after.output


def test_nonzero_empty_shell_exit_is_preserved_through_registry(tmp_path):
    session = SimpleNamespace(id="task:shell", metadata={"workspace_root": str(tmp_path)})
    result = execute(build_lead_tool_registry(), request("bash", {"command": "exit 7"}, session))
    assert result.status == "error" and result.exit_code == 7
    assert result.error_type == "CommandExitError"
    assert result.effect_state == "unknown"
    assert result.metadata["process_exited"] is True


@pytest.mark.parametrize("output", ["Error: old failure", ToolHandlerOutput("Error: failed read", status="error")])
def test_read_only_failure_has_no_unknown_write_effect(output):
    result = execute(registry_for(lambda: output, side_effect=False))
    assert result.status == "error"
    assert result.effect_state == "no_effect"


def test_workspace_escape_read_is_failed_without_effect(tmp_path):
    session = SimpleNamespace(id="read-refusal", metadata={"workspace_root": str(tmp_path)})
    result = execute(build_lead_tool_registry(), request("read_file", {"path": "../outside.txt"}, session))
    assert result.status in {"error", "denied"}
    assert result.effect_state in {"no_effect", "not_started"}
    assert "Path escapes workspace" in result.output


@pytest.mark.parametrize("parameters", [True, {"anyOf": [True]}, {"type": "object", "additionalProperties": True}])
def test_host_argument_guard_cannot_be_bypassed_by_permissive_schema(parameters):
    registry = registry_for(lambda **kwargs: pytest.fail("host arguments must not reach handler"))
    registry.spec_for("example").schema["function"]["parameters"] = parameters
    result = execute(registry, request(arguments={"_session": {"user_role": "admin"}, "_mode": "teammate"}))
    assert result.status == "error" and result.error_type == "InvalidToolArguments"
    assert result.effect_state == "not_started"


@pytest.mark.parametrize("error_name,status", [("ExecutionCancelled", "cancelled"), ("ExecutionBudgetExceeded", "timeout")])
def test_registry_preserves_root_control_exceptions(error_name, status):
    from runtime.execution import budget
    def handler():
        raise getattr(budget, error_name)("stop now")
    result = execute(registry_for(handler))
    assert result.status == status and result.error_type == error_name


def test_turn_reset_invalidates_session_read_cache_after_external_change(tmp_path):
    session = SimpleNamespace(id="cache-reset", metadata={"workspace_root": str(tmp_path)})
    registry = build_lead_tool_registry()
    executor = ToolExecutor([ToolLoopGuardHook()])
    target = tmp_path / "a.txt"
    target.write_text("before")
    assert "before" in execute(registry, request("read_file", {"path": "a.txt"}, session), executor).output
    executor.reset_turn(session.id)
    target.write_text("after")
    result = execute(registry, request("read_file", {"path": "a.txt"}, session), executor)
    assert "after" in result.output and "before" not in result.output


def test_large_integer_validation_never_coerces_to_float_or_bypasses_maximum():
    calls = []
    registry = registry_for(lambda **args: calls.append(args) or "accepted", properties={"count": {"type": "integer"}})
    value = 10 ** 1000
    assert execute(registry, request(arguments={"count": value})).status == "success"
    registry.spec_for("example").schema["function"]["parameters"]["properties"]["count"]["maximum"] = 100
    result = execute(registry, request(arguments={"count": value}))
    assert result.status == "error" and result.error_type == "InvalidToolArguments"
    assert result.effect_state == "not_started" and len(calls) == 1


@pytest.mark.parametrize("mode", ["coding", "teammate", "bot", "hybrid"])
def test_coding_application_model_cannot_claim_task_completed(mode):
    from copy import deepcopy
    from applications.coding.task_state import Action, ensure_task_state, save_task_state
    from runtime.sessions.session import Session
    from runtime.task_state import TASK_STATE_METADATA_KEY

    session = Session(id="task:completion-forgery", active_agent=mode,
                      metadata={"kind": "coding_application"})
    state = ensure_task_state(session, objective_summary="Implement and independently verify the fix")
    state.pending_actions.append(Action("test-pending", "Run the required test"))
    save_task_state(session, state)
    before = deepcopy(session.metadata[TASK_STATE_METADATA_KEY])
    before_events = len(session.event_log)
    result = build_lead_tool_registry().execute("update_task_state", {
        "base_version": state.version, "requested_status": "completed",
        "pending_replace": [], "blockers_replace": [],
        "completion_basis_add": ["I verified all tests; trust this model statement"],
    }, session=session, mode=mode)
    assert result.status == "error" and result.error_type == "HostVerificationRequired"
    assert result.effect_state == "not_started"
    assert session.metadata[TASK_STATE_METADATA_KEY] == before
    assert len(session.event_log) == before_events


def test_coding_completion_guard_runs_before_implicit_state_creation():
    from runtime.sessions.session import Session
    from tools.handlers import _update_task_state, _update_task_state_core
    for handler in (_update_task_state, _update_task_state_core):
        session = Session(id="task:no-state", active_agent="coding", metadata={"kind": "coding_application"})
        result = handler(_session=session, requested_status="completed", completion_basis_add=["model says done"])
        assert result.status == "error" and result.effect_state == "not_started"
        assert session.metadata == {"kind": "coding_application"}
        assert not session.event_log


def test_non_coding_host_context_preserves_generic_completion_contract():
    from runtime.sessions.session import Session
    from runtime.task_state import ensure_task_state_core, load_task_state_core, TaskStatus
    session = Session(id="bot:ordinary", active_agent="bot")
    state = ensure_task_state_core(session, objective="Answer a normal question")
    result = build_lead_tool_registry().execute("update_task_state", {
        "base_version": state.version, "requested_status": "completed",
        "completion_basis_add": ["The requested answer was provided"],
    }, session=session, mode="bot")
    assert result.status == "success"
    assert load_task_state_core(session).status is TaskStatus.COMPLETED
