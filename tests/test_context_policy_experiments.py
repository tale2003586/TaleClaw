from copy import deepcopy
import json
from types import SimpleNamespace as NS

import pytest

from applications.coding.context_state import build_coding_context_view, group_messages
from applications.coding.task_state import TaskState, Objective, save_task_state
from evaluation.context_experiments import run_matrix, summarize_cells
from runtime.context.policies import ContextPolicy, context_policy, plan_context, request_compaction, resolve_context_policy
from runtime.sessions.session import Session


def session_fixture():
    session = Session("offline:policies")
    session.add_message("user", "Fix the parser; preserve every public API and do not change lockfiles.")
    for index in range(6):
        session.add_message("assistant", None, tool_calls=[{"id": f"c{index}", "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"path": f"file{index}.py"})}}])
        session.add_message("tool", f"evidence-file{index}.py-" * 1000, tool_call_id=f"c{index}")
    return session


def test_masking_is_recoverable_and_never_mutates_source():
    session = session_fixture()
    before = deepcopy(session.messages)
    plan = plan_context(session, groups=group_messages(session.messages),
                        policy=ContextPolicy("masking", recent_groups=1), max_tokens=2000)
    masked = [m for m in plan.messages if (m.get("metadata") or {}).get("context_projection") == "masked"]
    assert masked and plan.after_tokens < plan.before_tokens
    events = {f"event://{e.event_id}": e for e in session.events_after(0)}
    for message in masked:
        envelope = json.loads(message["content"])
        original = events[envelope["source_ref"]].payload["message"]["content"]
        assert envelope["source_chars"] == len(original)
        assert envelope["preview"] == original[:320]
    assert session.messages == before
    raw = plan_context(session, groups=group_messages(session.messages), policy=ContextPolicy("raw"), max_tokens=1)
    assert raw.messages == before


def test_adaptive_rehydrates_old_relevant_observation_when_focus_changes():
    session = session_fixture()
    groups = group_messages(session.messages)
    raw_cost = plan_context(session, groups=groups[-1:], policy=ContextPolicy("raw"), max_tokens=1).after_tokens
    budget = raw_cost * 2 + 600
    first = plan_context(session, groups=groups, policy=ContextPolicy("adaptive", recent_groups=1),
                         max_tokens=budget, focus={"path": "file0.py"})
    assert any(row["start"] == 1 and row["mode"] == "raw" for row in first.decisions)
    second = plan_context(session, groups=groups, policy=ContextPolicy("adaptive", recent_groups=1),
                          max_tokens=budget, focus={"path": "file1.py"})
    assert any(row["start"] == 3 and row["mode"] == "raw" for row in second.decisions)
    assert any("evidence-file1.py-" * 100 in str(m["content"]) for m in second.messages)
    assert first.source_digest == second.source_digest


def test_open_transaction_and_full_user_instruction_survive_tiny_target():
    session = session_fixture()
    session.add_message("assistant", None, tool_calls=[{"id": "pending", "type": "function",
                        "function": {"name": "inspect", "arguments": "{}"}}])
    plan = plan_context(session, groups=group_messages(session.messages),
                        policy=ContextPolicy("adaptive", recent_groups=1), max_tokens=1)
    assert plan.messages[0] == session.messages[0]
    assert plan.messages[-1] == session.messages[-1]
    assert plan.audit()["over_target"] is True
    assert any(row["mode"] == "abstract" for row in plan.decisions)
    assert plan.audit()["execution_index"]["status"] == "unavailable_budget"


@pytest.mark.parametrize("name", ["masking", "adaptive"])
def test_no_recovery_address_means_observation_cannot_be_masked(name):
    session = session_fixture()
    session.events_after = lambda _: []
    plan = plan_context(session, groups=group_messages(session.messages),
                        policy=ContextPolicy(name), max_tokens=1)
    assert plan.messages == session.messages


@pytest.mark.parametrize("value", ["typo", {"name": "adaptive", "unknown": 1}, {"name": "raw", "recent_groups": 0},
                                  {"name": "native", "native_fallback": "silent"}, {"soft_ratio": .3, "target_ratio": .5}])
def test_invalid_policy_rejected(value):
    with pytest.raises((ValueError, TypeError)):
        context_policy(NS(metadata={"context_policy": value}))


def test_native_fallback_is_explicit_and_recomputed_after_config_change():
    provider = NS()
    session = Session("policy:fallback", metadata={"context_policy": "native"})
    from models.provider import NativeContextUnsupported
    with pytest.raises(NativeContextUnsupported):
        resolve_context_policy(session, provider)
    session.metadata["context_policy"] = {"name": "native", "native_fallback": "masking"}
    assert resolve_context_policy(session, provider).name == "masking"
    assert context_policy(session).name == "masking"
    assert session.metadata["context_policy_resolution"]["requested"] == "native"
    session.metadata["context_policy"] = "adaptive"
    assert context_policy(session).name == "adaptive"


def test_real_builder_uses_selected_policy_after_archive_and_task_continuation():
    session = session_fixture()
    session.archive_boundary_seq = 4
    session._refresh_active_event_window()
    session.metadata["context_policy"] = "masking"
    view = build_coding_context_view(session, objective="Fix parser",
        active_turn_start_index=len(session.messages)-2, static_messages=[], usable_input_tokens=20000)
    assert any((m.get("metadata") or {}).get("context_projection") == "masked" for m in view.recent_messages)
    assert session.metadata["context_policy_audit"]["policy"] == "masking"
    assert session.metadata["context_policy_audit"]["decisions"][0]["start"] == 0


def test_proactive_request_waits_for_closed_tool_boundary():
    session = session_fixture()
    session.metadata["context_policy"] = "adaptive"
    session.add_message("assistant", None, tool_calls=[{"id": "pending", "type": "function",
        "function": {"name": "compact", "arguments": "{}"}}])
    assert request_compaction(session, focus="parser failure")["accepted"]
    assert not request_compaction(session)["accepted"]
    build_coding_context_view(session, objective="Fix parser", active_turn_start_index=0,
                             static_messages=[], usable_input_tokens=10000)
    assert session.metadata["context_compaction_request"]["status"] == "pending"
    session.add_message("tool", "queued", tool_call_id="pending")
    build_coding_context_view(session, objective="Fix parser", active_turn_start_index=0,
                             static_messages=[], usable_input_tokens=10000)
    assert session.metadata["context_compaction_request"]["status"] == "completed"


def test_constraint_only_revision_does_not_replace_objective():
    session = Session("revision:policy")
    session.add_message("user", "Fix parser")
    state = TaskState(task_id=session.id, objective=Objective("Fix parser"))
    save_task_state(session, state)
    session.add_message("user", "Allow updating tests", metadata={"kind": "user_correction",
        "requirements_revision": {"remove_constraint_ids": ["no_tests"]}})
    view = build_coding_context_view(session, objective="Allow updating tests", active_turn_start_index=0,
                                    static_messages=[], usable_input_tokens=10000)
    assert view.task_state.objective.summary == "Fix parser"
    assert "explicit_field_revision" in view.state_message["content"]


def test_matrix_scripted_cost_is_unknown_and_native_unsupported(tmp_path):
    result = run_matrix(policies=("current", "masking", "adaptive", "native"), task_id="coding-git-diff-008",
                        eval_root=tmp_path, max_reasoning_steps=12)
    assert result["status"] == "completed"
    assert len(result["cells"]) == 4
    for policy in ("current", "masking", "adaptive"):
        assert result["summary"][policy]["verified_pass"] == 1
        assert result["summary"][policy]["total_tokens"] is None
    assert result["summary"]["native"]["unsupported_cells"] == 1
    assert result["summary"]["native"]["tasks"] == 0
    assert result["summary"]["native"]["pass_rate_all_attempts"] is None
    assert result["summary"]["native"]["planned_completion_rate"] == 0
    assert (tmp_path / result["id"] / "report.html").is_file()
    assert (tmp_path / result["id"] / "rows.csv").is_file()


def test_paired_report_does_not_convert_unknown_to_failure_or_zero_cost():
    def cell(name, outcome):
        return {"policy": {"name": name}, "repeat": 1, "runner_mode": "real", "status": "completed",
                "rows": [{"id": "a", "verification": {"outcome": outcome},
                          "execution_budget": {"known_tokens": 10, "usage_complete": False}}]}
    summary = summarize_cells([cell("current", "inconclusive"), cell("adaptive", "pass")])
    assert summary["current"]["verified_fail"] == 0
    assert summary["adaptive"]["paired_vs_current"]["valid_pairs"] == 0
    assert summary["adaptive"]["total_tokens"] is None
