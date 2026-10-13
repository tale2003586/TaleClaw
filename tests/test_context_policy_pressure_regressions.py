"""Pressure regressions through source grouping, projection and final guards."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from applications.coding.context_state import build_coding_context_view, group_messages
from runtime.context.dynamic_budget import calculate_dynamic_prompt_budget, enforce_hard_token_guard
from runtime.context.evidence_recovery import read_session_evidence
from runtime.context.events import is_real_user_message
from runtime.context.policies import ContextPolicy, plan_context
from runtime.sessions.session import Session
from runtime.token_estimator import estimate_tokens


def pressure_session():
    session = Session("offline:protected-assistant-pressure")
    session.add_message("user", "Repair parser. Preserve the public API and do not edit lockfiles.")
    session.add_message("assistant", "COLD_ASSISTANT_DIAGNOSTIC " + "old explanatory note " * 18_000)
    for index in range(4):
        add_transaction(session, str(index))
    return session


def add_transaction(session, call_id):
    session.add_message("assistant", None, tool_calls=[{"id": call_id, "type": "function",
        "function": {"name": "inspect", "arguments": "{}"}}])
    session.add_message("tool", "routine result " + call_id, tool_call_id=call_id)


def test_short_user_request_does_not_pin_an_unbounded_old_assistant_reply():
    session = pressure_session()
    original = deepcopy(session.messages)
    plan = plan_context(session, groups=group_messages(session.messages),
        policy=ContextPolicy("adaptive", recent_groups=1), max_tokens=1_000)
    assert plan.before_tokens > 50_000
    assert plan.after_tokens <= 1_000
    assert [m for m in plan.messages if is_real_user_message(m)] == [original[0]]
    assert session.messages == original
    discarded = next(row for row in plan.decisions if row["start"] == 1)
    assert discarded["end"] == 2 and discarded["mode"] == "drop"
    recovered, _ = read_session_evidence(discarded["source_refs"][0], session=session,
                                         query="COLD_ASSISTANT_DIAGNOSTIC")
    assert json.loads(recovered)["matches"]


def test_real_coding_view_passes_final_guard_without_truncating_user_sources():
    session = pressure_session()
    session.metadata["context_policy"] = {"name": "adaptive", "recent_groups": 1}
    view = build_coding_context_view(session, objective=session.messages[0]["content"],
        active_turn_start_index=0, static_messages=[], usable_input_tokens=4_000)
    actual_prompt = [view.state_message, *view.recent_messages]
    budget = calculate_dynamic_prompt_budget(provider=SimpleNamespace(context_window_tokens=6_000),
                                              reserved_output_tokens=200)
    enforce_hard_token_guard(actual_prompt, budget=budget)
    assert session.messages[0] in view.recent_messages
    assert "COLD_ASSISTANT_DIAGNOSTIC" not in str(actual_prompt)
    assert estimate_tokens(actual_prompt) < 3_000


def test_twenty_four_turns_bound_assistant_history_and_retain_every_user_correction():
    session = pressure_session()
    sizes = []
    for index in range(24):
        session.add_message("user", f"Correction {index}: preserve required behavior {index}.")
        session.add_message("assistant", f"old explanation {index} " * 600)
        add_transaction(session, f"next-{index}")
        plan = plan_context(session, groups=group_messages(session.messages),
            policy=ContextPolicy("adaptive", recent_groups=1), max_tokens=3_000)
        assert [m for m in plan.messages if is_real_user_message(m)] == [
            m for m in session.messages if is_real_user_message(m)]
        sizes.append(plan.after_tokens)
    assert max(sizes) <= 3_000
    assert len(session.messages) == 106


@pytest.mark.parametrize("policy", ["current", "raw", "native", "masking"])
def test_other_policies_keep_their_existing_turn_group_semantics(policy):
    session = pressure_session()
    groups = group_messages(session.messages)
    plan = plan_context(session, groups=groups, policy=ContextPolicy(policy, recent_groups=1), max_tokens=1_000)
    assert plan.decisions[0]["start"] == 0 and plan.decisions[0]["end"] == 2
    assert session.messages[1] in plan.messages


def test_unrecoverable_and_recent_assistant_turns_remain_protected():
    session = pressure_session()
    session.events_after = lambda _: []
    plan = plan_context(session, groups=group_messages(session.messages),
        policy=ContextPolicy("adaptive", recent_groups=1), max_tokens=1)
    assert plan.messages == session.messages
    session = Session("offline:recent-turn")
    add_transaction(session, "recent-transaction")
    session.add_message("user", "Newest request")
    session.add_message("assistant", "Newest answer")
    groups = group_messages(session.messages)
    plan = plan_context(session, groups=groups, policy=ContextPolicy("adaptive", recent_groups=2), max_tokens=1)
    assert plan.messages == session.messages
