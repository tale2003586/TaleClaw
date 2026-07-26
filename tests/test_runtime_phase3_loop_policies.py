from __future__ import annotations

from types import SimpleNamespace

from agents.definitions import BOT_AGENT_SPEC
from agents.definitions import CODING_AGENT_SPEC
from runtime.execution.loop_policies import (
    FINISHING_REMINDER_SENT_KEY,
    WEB_SEARCH_BUDGET_REMAINING_KEY,
    WEB_SEARCH_BUDGET_USED_KEY,
    FinishingReminderPolicy,
    ToolBatchPolicy,
    WebSearchBudgetPolicy,
    WorkingMemoryPolicy,
)
from runtime.sessions import Session


def test_web_search_budget_policy_preserves_denial_and_notice_contract():
    policy = WebSearchBudgetPolicy()
    session = Session(
        id="phase3:web",
        metadata={
            "web_search_budget_limit": 1,
            "web_search_budget_used": 0,
            "web_search_budget_remaining": 1,
        },
    )

    assert policy.denial(session, "web_search") == ""
    assert session.metadata[WEB_SEARCH_BUDGET_USED_KEY] == 1
    assert session.metadata[WEB_SEARCH_BUDGET_REMAINING_KEY] == 0
    assert 'remaining="0"' in policy.add_notice(
        session,
        "web_search",
        "result",
    )
    assert "budget exhausted" in policy.denial(session, "web_search")


def test_finishing_policy_injects_once_with_same_trace_payload():
    events = []
    session = Session(id="phase3:finishing")
    policy = FinishingReminderPolicy(max_reasoning_steps=10)

    policy.inject(
        session,
        policy._reminder_step(),
        trace=lambda event, payload: events.append((event, payload)),
    )
    policy.inject(
        session,
        10,
        trace=lambda event, payload: events.append((event, payload)),
    )

    reminders = [
        item for item in session.messages
        if item.get("metadata", {}).get("kind") == "runtime_finishing_reminder"
    ]
    assert len(reminders) == 1
    assert session.metadata[FINISHING_REMINDER_SENT_KEY]
    assert events[0][0] == "reasoning.finishing_reminder.injected"
    assert len(events) == 1


def test_tool_batch_policy_only_selects_homogeneous_bounded_calls():
    policy = ToolBatchPolicy()
    tasks = [SimpleNamespace(name="task"), SimpleNamespace(name="task")]
    reads = [SimpleNamespace(name="read_file"), SimpleNamespace(name="read_file")]

    assert policy.should_parallelize_tasks(tasks, available=True)
    assert not policy.should_parallelize_tasks(tasks, available=False)
    assert policy.should_batch_reads(reads, available=True)
    assert not policy.should_batch_reads([*reads, SimpleNamespace(name="task")], available=True)
    assert not policy.should_batch_reads(reads * 5, available=True)


def test_working_memory_policy_is_explicitly_scoped_to_coding(monkeypatch):
    policy = WorkingMemoryPolicy()
    monkeypatch.setattr("runtime.execution.loop_policies.WORKING_MEMORY_CHECKPOINT_ENABLED", True)

    assert policy.enabled_for(CODING_AGENT_SPEC)
    assert not policy.enabled_for(BOT_AGENT_SPEC)
