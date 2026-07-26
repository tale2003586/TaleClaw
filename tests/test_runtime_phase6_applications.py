from __future__ import annotations

from types import SimpleNamespace

from applications.coding.runner import CodingApplication
from runtime.execution.child_run import ChildRun
from runtime.extensions import RuntimeExtensions
from runtime.runtime import RunContext
from runtime.sessions import Session


def test_child_run_has_independent_identity_and_explicit_parent():
    child = ChildRun.create(SimpleNamespace(run_id="parent-run"))
    state = child.execution_state()

    assert child.run_id.startswith("child_")
    assert child.run_id != "parent-run"
    assert state.run_id == child.run_id
    assert state.parent_run_id == "parent-run"


def test_core_run_context_has_no_optional_extensions_by_default():
    context = RunContext(session=Session(id="phase6:extensions"))

    assert context.extensions == RuntimeExtensions()
    assert context.extensions.enabled() == ()


def test_application_can_compose_optional_extensions_explicitly():
    trace = object()
    memory = object()
    context = RunContext(
        session=Session(id="phase6:composed"),
        extensions=RuntimeExtensions(trace=trace, memory=memory),
    )

    assert context.extensions.trace is trace
    assert context.extensions.memory is memory
    assert context.extensions.enabled() == ("memory", "trace")
