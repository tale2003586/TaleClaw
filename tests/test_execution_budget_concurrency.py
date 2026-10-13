"""Deterministic scheduling checks for the shared execution budget."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

import runtime.execution.budget as budget_module
from runtime.execution.budget import ExecutionBudget, ExecutionBudgetExceeded


@pytest.mark.parametrize("kind", ["model", "tool"])
@pytest.mark.parametrize("exhausted", ["tokens", "deadline"])
def test_claim_rechecks_limits_after_another_thread_consumes_budget(monkeypatch, kind, exhausted):
    clock = [100.0]
    monkeypatch.setattr(budget_module, "monotonic", lambda: clock[0])
    budget = ExecutionBudget(deadline=101.0, max_known_tokens=10)
    checked, resume = Event(), Event()
    check = budget.check

    def pause_after_check():
        check()
        checked.set()
        assert resume.wait(5), "test did not release the paused claimant"

    # Force preemption after the unlocked preflight, before claim acquires its
    # accounting lock. No timing sleeps or probabilistic thread races are used.
    monkeypatch.setattr(budget, "check", pause_after_check)
    with ThreadPoolExecutor(max_workers=1) as pool:
        claimed = pool.submit(budget.claim, kind)
        try:
            assert checked.wait(5), "claim did not finish its preflight"
            if exhausted == "tokens":
                budget.record_usage(SimpleNamespace(total_tokens=10))
            else:
                clock[0] = 101.0
        finally:
            resume.set()
        with pytest.raises(ExecutionBudgetExceeded):
            claimed.result(timeout=5)

    assert budget.model_attempts == 0
    assert budget.tool_attempts == 0
