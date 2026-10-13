from copy import deepcopy
from hashlib import sha256

import pytest

from applications.coding.evidence_validity import refresh_evidence_validity
from applications.coding.reasoning_memory import (
    invalidate_reasoning_for_revision,
    reasoning_context_view,
)
from applications.coding.state_updates import (
    DeterministicEventExtractor, StatePatch, StateValidationError, reduce_task_state,
)
from applications.coding.task_state import (
    Action, CompletedItem, Decision, EvidenceRef, Finding, Hypothesis,
    ItemStatus, Objective, TaskState,
)


def observation(ref="read", **kwargs):
    return EvidenceRef(ref, f"event-{ref}", kind="tool_result", tool_name="read_file",
                       provenance="host_tool_result", task_id="task-a", **kwargs)


def task(*refs):
    return TaskState(task_id="task-a", objective=Objective("repair parser"),
                     evidence_index={ref.id: ref for ref in refs})


def test_legacy_hypothesis_roundtrips_without_becoming_verified():
    state = task()
    state.hypotheses = [Hypothesis("old", "maybe CRLF", status=ItemStatus.COMPLETED)]
    restored = TaskState.from_payload(state.to_dict())
    assert restored.hypotheses[0].lifecycle == "open"
    assert reasoning_context_view(restored)["interpretation_origin"] == "model_reasoning_not_tool_verification"


def test_open_hypothesis_records_prediction_and_next_validation_without_evidence():
    updated = reduce_task_state(task(), StatePatch.from_payload({"add_hypotheses": [{
        "id": "h", "claim": "CRLF causes offset", "falsifiable_prediction": "LF-only input succeeds",
        "next_validation": "run LF and CRLF fixtures", "lifecycle": "open",
    }]}))
    assert updated.hypotheses[0].next_validation == "run LF and CRLF fixtures"
    assert updated.hypotheses[0].task_id == "task-a"
    assert updated.hypotheses[0].lifecycle == "open"


@pytest.mark.parametrize("lifecycle", ["confirmed", "excluded"])
def test_empty_evidence_cannot_close_hypothesis(lifecycle):
    with pytest.raises(StateValidationError, match="fresh host"):
        reduce_task_state(task(), StatePatch(hypotheses=[Hypothesis(
            "h", "CRLF", lifecycle=lifecycle, falsifiable_prediction="LF succeeds")]))


def test_host_observation_can_support_model_confirmation_but_does_not_create_verification():
    state = reduce_task_state(task(observation()), StatePatch(hypotheses=[Hypothesis(
        "h", "CRLF", lifecycle="confirmed", supporting_evidence_refs=["read"],
        falsifiable_prediction="offset differs only for CRLF")]))
    assert state.hypotheses[0].lifecycle == "confirmed"
    assert not state.evidence_index["read"].verification


def test_refutation_is_not_promoted_to_a_positive_finding():
    state = reduce_task_state(task(observation()), StatePatch(hypotheses=[Hypothesis(
        "h", "CRLF", lifecycle="excluded", refuting_evidence_refs=["read"],
        falsifiable_prediction="LF succeeds")]))
    with pytest.raises(StateValidationError, match="excluded hypothesis"):
        reduce_task_state(state, StatePatch(findings=[Finding(
            "f", "CRLF is the cause", ["read"], dependencies=["hypothesis:h"])]))


def test_one_observation_cannot_be_both_support_and_refutation():
    with pytest.raises(StateValidationError, match="both support and refute"):
        reduce_task_state(task(observation()), StatePatch(hypotheses=[Hypothesis(
            "h", "CRLF", supporting_evidence_refs=["read"], refuting_evidence_refs=["read"])]))


def test_dependency_cycle_between_finding_and_decision_is_rejected():
    with pytest.raises(StateValidationError, match="cycle"):
        reduce_task_state(task(observation()), StatePatch(
            findings=[Finding("f", "offset wrong", ["read"], dependencies=["decision:d"])],
            decisions=[Decision("d", "normalize", related_findings=["f"])],
        ))


@pytest.mark.parametrize("foreign", ["evidence", "item", "dependency"])
def test_task_scope_is_checked_even_for_locally_indexed_foreign_ids(foreign):
    state = task(observation())
    if foreign == "evidence":
        state.evidence_index["read"].task_id = "task-b"
    item = Finding("f", "offset wrong", ["read"], task_id="task-b" if foreign == "item" else "")
    if foreign == "dependency":
        state.findings = [Finding("old", "prior task", ["read"], task_id="task-b")]
        item.dependencies = ["finding:old"]
    with pytest.raises(StateValidationError, match="another task"):
        reduce_task_state(state, StatePatch(findings=[item]))


def test_external_edit_invalidates_evidence_finding_decision_action_and_completion(tmp_path):
    path = tmp_path / "parser.py"
    path.write_text("old")
    state = task(observation(file_dependencies={"parser.py": sha256(b"old").hexdigest()}, workspace_root=str(tmp_path)))
    state.findings = [Finding("f", "offset wrong", ["read"])]
    state.decisions = [Decision("d", "normalize", related_findings=["f"])]
    state.pending_actions = [Action("a", "apply fix", ItemStatus.COMPLETED, dependencies=["decision:d"])]
    state.completed = [CompletedItem("c", "fixed", dependencies=["action:a"])]
    state.hypotheses = [Hypothesis("h", "CRLF", supporting_evidence_refs=["read"], lifecycle="confirmed")]
    path.write_text("new")
    updated = refresh_evidence_validity(state, tmp_path)
    for item in [*updated.findings, *updated.decisions, *updated.pending_actions]:
        assert item.status == ItemStatus.AWAITING_VERIFICATION
        assert item.revalidation_required
    assert updated.completed[0].needs_parent_verification
    assert updated.hypotheses[0].lifecycle == "stale"
    assert not state.evidence_index["read"].stale


def test_pending_replace_cannot_launder_host_invalidated_action():
    state = task(observation())
    state.pending_actions = [Action("a", "apply fix", ItemStatus.AWAITING_VERIFICATION,
                                   evidence_refs=["read"], revalidation_required=True)]
    with pytest.raises(StateValidationError, match="new fresh host evidence"):
        reduce_task_state(state, StatePatch(pending_replace=[Action("a", "already complete", ItemStatus.COMPLETED)]))


def test_new_host_evidence_can_revalidate_a_stale_hypothesis():
    state = task(observation("old", stale=True), observation("fresh"))
    state.hypotheses = [Hypothesis("h", "CRLF", supporting_evidence_refs=["old"],
                                  lifecycle="stale", revalidation_required=True)]
    updated = reduce_task_state(state, StatePatch(hypotheses=[Hypothesis(
        "h", "CRLF", supersedes="h", supporting_evidence_refs=["fresh"],
        lifecycle="confirmed", falsifiable_prediction="LF fixture succeeds")]))
    assert updated.hypotheses[0].lifecycle == "confirmed"
    assert not updated.hypotheses[0].revalidation_required


def test_removing_old_counterevidence_requires_new_host_support():
    state = task(observation("negative"))
    state.hypotheses = [Hypothesis("h", "CRLF", lifecycle="excluded", refuting_evidence_refs=["negative"])]
    with pytest.raises(StateValidationError, match="new host support"):
        reduce_task_state(state, StatePatch(hypotheses=[Hypothesis(
            "h", "CRLF", supersedes="h", lifecycle="confirmed", supporting_evidence_refs=["negative"],
            falsifiable_prediction="LF fixture succeeds")]))


@pytest.mark.parametrize("field,value", [("verification", True), ("provenance", "host_tool_result"),
                                          ("file_dependencies", {"a.py": "fake"}), ("tool_name", "bash")])
def test_real_model_tool_route_cannot_forge_host_observation(field, value):
    from runtime.sessions.session import Session
    from tools.handlers import TASK_HANDLERS
    session = Session(id=f"forge-{field}", active_agent="coding")
    session.add_message("user", "repair parser")
    with pytest.raises(StateValidationError, match="cannot create host"):
        TASK_HANDLERS["update_task_state"](_session=session, origin="deterministic", add_evidence=[{
            "id": "fake", "event_id": "fake-event", field: value,
        }])


def test_model_reference_payload_stays_compatible_but_cannot_confirm_hypothesis():
    state = reduce_task_state(task(), StatePatch(origin="tool", evidence=[EvidenceRef("m", "not-real")]))
    assert state.evidence_index["m"].provenance == "model_reference"
    with pytest.raises(StateValidationError, match="fresh host"):
        reduce_task_state(state, StatePatch(hypotheses=[Hypothesis(
            "h", "claims pass", lifecycle="confirmed", supporting_evidence_refs=["m"],
            falsifiable_prediction="test passes")]))


def test_task_state_tool_receipt_is_not_independent_evidence_of_its_own_claim():
    ref = observation()
    ref.tool_name = "update_task_state"
    with pytest.raises(StateValidationError, match="fresh host"):
        reduce_task_state(task(ref), StatePatch(hypotheses=[Hypothesis(
            "h", "claims pass", lifecycle="confirmed", supporting_evidence_refs=["read"],
            falsifiable_prediction="test passes")]))


def test_model_tool_cannot_record_fake_test_execution():
    with pytest.raises(StateValidationError, match="execution or coverage"):
        reduce_task_state(task(), StatePatch(origin="tool", tests_run=["fake-pass"]))


def test_real_extractor_marks_observations_as_host_origin():
    patch = DeterministicEventExtractor().extract([{"event_id": "e", "event_type": "tool_result",
        "payload": {"tool_name": "bash", "content": "0 passed", "arguments": {"command": "pytest"}}}])
    assert patch.evidence[0].provenance == "host_tool_result"
    assert patch.evidence[0].verification


def test_revised_finding_invalidates_downstream_decision_without_file_change():
    state = task(observation())
    state.findings = [Finding("f", "old interpretation", ["read"])]
    state.decisions = [Decision("d", "keep offset", related_findings=["f"])]
    updated = reduce_task_state(state, StatePatch(findings=[Finding(
        "f", "new interpretation", ["read"], supersedes="f")]))
    assert updated.decisions[0].revalidation_required
    assert updated.decisions[0].status == ItemStatus.AWAITING_VERIFICATION


def test_requirements_revision_preserves_raw_observations_but_rechecks_interpretation():
    state = task(observation())
    state.hypotheses = [Hypothesis("h", "CRLF", lifecycle="confirmed", supporting_evidence_refs=["read"])]
    assert invalidate_reasoning_for_revision(state)
    assert state.hypotheses[0].lifecycle == "stale"
    assert not state.evidence_index["read"].stale


def test_planner_projection_is_bounded_and_keeps_cold_recovery_ref():
    state = task(observation())
    state.hypotheses = [Hypothesis(str(i), "x" * 1000, next_validation="y" * 1000) for i in range(20)]
    view = reasoning_context_view(state, max_items=3)
    assert len(view["hypotheses"]) == 3
    assert view["omitted_hypotheses"] == 17
    assert len(view["hypotheses"][0]["claim"]) == 600
    assert view["full_state_ref"] == "state://current"


def test_same_patch_cannot_change_prerequisite_then_finalize_stale_completion():
    from runtime.task_state.models import TaskStatus
    state = task(observation())
    state.findings = [Finding("f", "old interpretation", ["read"])]
    state.completed = [CompletedItem("done", "fixed", dependencies=["finding:f"])]
    with pytest.raises(StateValidationError, match="requiring revalidation"):
        reduce_task_state(state, StatePatch(findings=[Finding(
            "f", "new interpretation", ["read"], supersedes="f")], requested_status=TaskStatus.COMPLETED, completion_basis_add=["read"]))


def test_same_patch_write_effect_cannot_leave_old_claim_confirmed():
    state = task(observation(path="a.py"))
    write = EvidenceRef("write", "event-write", kind="tool_result", provenance="host_tool_result",
                        tool_name="write_file", path="a.py", effect_state="applied")
    with pytest.raises(StateValidationError, match="stale evidence dependency chain"):
        reduce_task_state(state, StatePatch(evidence=[write], hypotheses=[Hypothesis(
            "h", "old bytes", lifecycle="confirmed", supporting_evidence_refs=["read"],
            falsifiable_prediction="read a.py shows old bytes")]))


def test_native_coding_state_and_observation_scope_bind_to_durable_task_identity():
    from runtime.sessions.session import Session
    from applications.coding.task_state import ensure_task_state, save_task_state
    session = Session(id="task:native", metadata={"task_id": "native"})
    state = ensure_task_state(session, objective_summary="repair")
    assert state.task_id == "native"
    state.evidence_index["old"] = EvidenceRef("old", "prior")
    save_task_state(session, state)
    assert state.evidence_index["old"].task_id == "native"
    foreign = deepcopy(state)
    foreign.task_id = "other"
    with pytest.raises(ValueError, match="different coding task"):
        save_task_state(session, foreign)


def test_file_invalidation_retires_stale_completion_basis_then_allows_fresh_recheck(tmp_path):
    from runtime.task_state.models import TaskStatus
    path = tmp_path / "parser.py"
    path.write_text("old")
    state = task(observation(file_dependencies={"parser.py": sha256(b"old").hexdigest()}, workspace_root=str(tmp_path)))
    state.completion_basis = ["read"]
    state.completed = [CompletedItem("done", "checked", ["read"])]
    path.write_text("new")
    state = refresh_evidence_validity(state, tmp_path)
    assert state.completion_basis == []
    assert state.history[-1].previous == {"refs": ["read"]}
    state.evidence_index["fresh"] = observation("fresh")
    state = reduce_task_state(state, StatePatch(
        completed=[CompletedItem("done", "checked again", ["fresh"], supersedes="done")],
        completion_basis_add=["fresh"], requested_status=TaskStatus.COMPLETED,
    ))
    assert state.status == TaskStatus.COMPLETED


def test_real_task_state_tool_persists_structured_hypothesis_and_roundtrips_scope():
    from runtime.sessions.session import Session
    from applications.coding.task_state import load_task_state, save_task_state
    from tools.handlers import TASK_HANDLERS
    session = Session(id="task:task-a", active_agent="coding", metadata={"task_id": "task-a"})
    session.add_message("user", "repair parser")
    save_task_state(session, task(observation()))
    TASK_HANDLERS["update_task_state"](_session=session, add_hypotheses=[{
        "id": "h", "claim": "CRLF causes offset", "lifecycle": "confirmed",
        "supporting_evidence_refs": ["read"], "falsifiable_prediction": "LF succeeds",
        "next_validation": "add CRLF regression",
    }])
    saved = load_task_state(session)
    assert saved.hypotheses[0].lifecycle == "confirmed"
    assert saved.hypotheses[0].task_id == "task-a"
    assert saved.evidence_index["read"].provenance == "host_tool_result"


def test_ordered_write_then_fresh_read_in_one_batch_does_not_invalidate_future_evidence():
    state = task(observation(path="a.py"))
    write = EvidenceRef("write", "event-write", kind="tool_result", provenance="host_tool_result",
                        tool_name="write_file", path="a.py", effect_state="applied")
    fresh = observation("fresh", path="a.py")
    updated = reduce_task_state(state, StatePatch(evidence=[write, fresh], hypotheses=[Hypothesis(
        "h", "new bytes", lifecycle="confirmed", supporting_evidence_refs=["fresh"],
        falsifiable_prediction="read a.py shows new bytes")]))
    assert updated.evidence_index["read"].stale
    assert not updated.evidence_index["fresh"].stale
    assert updated.hypotheses[0].lifecycle == "confirmed"
