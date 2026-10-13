from hashlib import sha256
from types import SimpleNamespace

from applications.coding.state_updates import DeterministicEventExtractor
from tools.handlers import run_read, run_read_files
from tools.executor import ToolExecutor, ToolExecutionRequest
from tools.hooks import ToolLoopGuardHook


def session_at(tmp_path):
    return SimpleNamespace(id="version-test", metadata={"workspace_root": str(tmp_path)})


def test_file_read_binds_exact_bytes_and_keeps_metadata_on_cache(tmp_path):
    (tmp_path / "a.py").write_bytes(b"one\r\ntwo\r\n")
    session = session_at(tmp_path)
    for _ in range(2):
        output = run_read("a.py", limit=1, _session=session)
        observations = getattr(output, "metadata", {}).get("file_observations", [])
        assert observations == [{"path": "a.py", "sha256": sha256(b"one\r\ntwo\r\n").hexdigest(), "workspace_root": str(tmp_path)}]


def test_external_edit_invalidates_file_read_cache(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("before-change")
    session = session_at(tmp_path)
    run_read("a.py", _session=session)
    path.write_text("after-change")
    assert "after-change" in run_read("a.py", _session=session)


def test_loop_guard_cannot_replay_stale_file_window(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("before-change")
    session = session_at(tmp_path)
    executor = ToolExecutor([ToolLoopGuardHook()])
    request = ToolExecutionRequest("read", "read_file", {"path": "a.py"}, session_id=session.id, metadata=session.metadata, session=session)
    invoke = lambda name, args: run_read(**args, _session=session)
    executor.execute(request, invoke)
    path.write_text("after-change")
    assert "after-change" in executor.execute(request, invoke).output


def test_batch_cache_validates_each_file(tmp_path):
    (tmp_path / "a.py").write_text("a-before")
    (tmp_path / "b.py").write_text("b-before")
    session = session_at(tmp_path)
    files = [{"path": "a.py"}, {"path": "b.py"}]
    run_read_files(files, _session=session)
    (tmp_path / "b.py").write_text("b-after")
    assert "b-after" in run_read_files(files, _session=session)


def test_tool_result_hash_is_read_from_actual_store_metadata():
    patch = DeterministicEventExtractor().extract([{
        "event_id": "read-1", "event_type": "tool_result", "payload": {
            "content": "observed", "metadata": {
                "tool_result_ref": {"uri": "artifact://result", "sha256": "output-hash"},
            },
        },
    }])
    assert patch.evidence[0].content_hash == "output-hash"


def state_with_observation(tmp_path):
    from applications.coding.state_updates import reduce_task_state
    from applications.coding.task_state import TaskState, Objective, Finding, CompletedItem
    session = session_at(tmp_path)
    output = run_read("a.py", _session=session)
    patch = DeterministicEventExtractor().extract([{
        "event_id": "read-1", "event_type": "tool_result", "payload": {
            "content": str(output), "tool_name": "read_file", "arguments": {"path": "a.py"},
            "metadata": output.metadata,
        },
    }])
    state = reduce_task_state(TaskState(objective=Objective("repair")), patch)
    state.findings.append(Finding("finding", "current behavior", ["evidence:read-1"]))
    state.completed.append(CompletedItem("completed", "verified behavior", ["evidence:read-1"]))
    return state


def test_external_change_marks_dependent_claims_stale_and_persists(tmp_path):
    from applications.coding.evidence_validity import refresh_evidence_validity
    from applications.coding.task_state import TaskState, ItemStatus
    path = tmp_path / "a.py"
    path.write_text("before")
    state = state_with_observation(tmp_path)
    assert refresh_evidence_validity(state, tmp_path) is state
    path.write_text("after")
    updated = refresh_evidence_validity(state, tmp_path)
    assert not state.evidence_index["evidence:read-1"].stale
    assert updated.evidence_index["evidence:read-1"].stale
    assert updated.findings[0].status == ItemStatus.AWAITING_VERIFICATION
    assert updated.completed[0].needs_parent_verification
    assert TaskState.from_payload(updated.to_dict()).evidence_index["evidence:read-1"].stale
    path.write_text("before")
    assert refresh_evidence_validity(updated, tmp_path).evidence_index["evidence:read-1"].stale


def test_stale_evidence_replay_cannot_support_new_completed_claim(tmp_path):
    import copy
    import pytest
    from applications.coding.evidence_validity import refresh_evidence_validity
    from applications.coding.state_updates import StatePatch, validate_state_patch, StateValidationError
    from applications.coding.task_state import CompletedItem
    path = tmp_path / "a.py"
    path.write_text("before")
    state = state_with_observation(tmp_path)
    original = copy.deepcopy(state.evidence_index["evidence:read-1"])
    path.write_text("after")
    stale = refresh_evidence_validity(state, tmp_path)
    patch = StatePatch(evidence=[original], completed=[CompletedItem("new", "done", [original.id])])
    with pytest.raises(StateValidationError, match="stale"):
        validate_state_patch(stale, patch)


def test_ordered_writes_invalidate_only_earlier_verification():
    from applications.coding.state_updates import reduce_task_state
    from applications.coding.task_state import TaskState, Objective
    events = []
    for event_id, name, args, effect in [
        ("test-before", "bash", {"command": "pytest -q"}, "unknown"),
        ("write", "write_file", {"path": "a.py"}, "applied"),
        ("test-after", "bash", {"command": "pytest -q"}, "unknown"),
    ]:
        events.append({"event_id": event_id, "event_type": "tool_result", "payload": {
            "tool_name": name, "arguments": args, "content": "done",
            "metadata": {"effect_state": effect},
        }})
    state = reduce_task_state(TaskState(objective=Objective("repair")), DeterministicEventExtractor().extract(events))
    assert state.evidence_index["evidence:test-before"].stale
    assert not state.evidence_index["evidence:test-after"].stale


def test_denied_write_does_not_invalidate_prior_verification():
    from applications.coding.evidence_validity import invalidate_after_tool_effect
    from applications.coding.task_state import TaskState, Objective, EvidenceRef
    prior = EvidenceRef("test", "test-event", verification=True)
    state = TaskState(objective=Objective("repair"), evidence_index={prior.id: prior})
    invalidate_after_tool_effect(state, EvidenceRef("write", "write-event", tool_name="write_file", effect_state="not_started"))
    assert not prior.stale


def test_cached_executor_preserves_host_observation_metadata(tmp_path):
    (tmp_path / "a.py").write_text("unchanged")
    session = session_at(tmp_path)
    executor = ToolExecutor([ToolLoopGuardHook()])
    request = ToolExecutionRequest("read", "read_file", {"path": "a.py"}, session_id=session.id, metadata=session.metadata, session=session)
    invoke = lambda name, args: run_read(**args, _session=session)
    first = executor.execute(request, invoke)
    second = executor.execute(request, invoke)
    assert second.metadata["file_observations"] == first.metadata["file_observations"]


def test_stale_evidence_cannot_complete_existing_action():
    import pytest
    from applications.coding.state_updates import StatePatch, ItemTransition, validate_state_patch, StateValidationError
    from applications.coding.task_state import TaskState, Objective, EvidenceRef, Action, ItemStatus
    state = TaskState(objective=Objective("repair"),
        evidence_index={"old": EvidenceRef("old", "event", stale=True)},
        pending_actions=[Action("action", "fix", ItemStatus.AWAITING_VERIFICATION, ["old"])])
    with pytest.raises(StateValidationError, match="stale"):
        validate_state_patch(state, StatePatch(origin="semantic", action_transitions=[ItemTransition("action", ItemStatus.COMPLETED)]))


def test_terminal_transition_rejects_existing_stale_completion():
    import pytest
    from applications.coding.state_updates import StatePatch, validate_state_patch, StateValidationError
    from applications.coding.task_state import TaskState, Objective, EvidenceRef, CompletedItem
    from runtime.task_state.models import TaskStatus
    state = TaskState(objective=Objective("repair"),
        evidence_index={"old": EvidenceRef("old", "event", stale=True)},
        completed=[CompletedItem("done", "passed", ["old"], needs_parent_verification=True)])
    with pytest.raises(StateValidationError, match="stale"):
        validate_state_patch(state, StatePatch(origin="semantic", requested_status=TaskStatus.COMPLETED, completion_basis_add=["already done"]))


def test_stale_completion_cannot_be_laundered_by_dropping_references():
    import pytest
    from applications.coding.state_updates import StatePatch, validate_state_patch, StateValidationError
    from applications.coding.task_state import TaskState, Objective, EvidenceRef, CompletedItem
    from runtime.task_state.models import TaskStatus
    state = TaskState(objective=Objective("repair"),
        evidence_index={"old": EvidenceRef("old", "event", stale=True)},
        completed=[CompletedItem("done", "passed", ["old"], needs_parent_verification=True)])
    for replacement_id in ("done", "replacement"):
        replacement = CompletedItem(replacement_id, "passed again", [], supersedes="done")
        with pytest.raises(StateValidationError, match="fresh evidence"):
            validate_state_patch(state, StatePatch(origin="semantic", completed=[replacement], requested_status=TaskStatus.COMPLETED, completion_basis_add=["done"]))


def test_fresh_evidence_can_replace_stale_completion():
    from applications.coding.state_updates import StatePatch, reduce_task_state
    from applications.coding.task_state import TaskState, Objective, EvidenceRef, CompletedItem
    from runtime.task_state.models import TaskStatus
    state = TaskState(objective=Objective("repair"),
        evidence_index={"old": EvidenceRef("old", "event", stale=True), "new": EvidenceRef("new", "new-event")},
        completed=[CompletedItem("done", "passed", ["old"], needs_parent_verification=True)])
    updated = reduce_task_state(state, StatePatch(origin="semantic",
        completed=[CompletedItem("replacement", "revalidated", ["new"], supersedes="done")],
        requested_status=TaskStatus.COMPLETED, completion_basis_add=["new"]))
    assert updated.status == TaskStatus.COMPLETED


def test_same_patch_supersedes_chain_cannot_launder_stale_completion():
    import pytest
    from applications.coding.state_updates import StatePatch, validate_state_patch, StateValidationError
    from applications.coding.task_state import TaskState, Objective, EvidenceRef, CompletedItem
    from runtime.task_state.models import TaskStatus
    state = TaskState(objective=Objective("repair"),
        evidence_index={"old": EvidenceRef("old", "event", stale=True)},
        completed=[CompletedItem("done", "passed", ["old"], needs_parent_verification=True)])
    with pytest.raises(StateValidationError, match="same-patch chain"):
        validate_state_patch(state, StatePatch(origin="semantic", completed=[
            CompletedItem("bridge", "pending", [], supersedes="done", needs_parent_verification=True),
            CompletedItem("fresh", "done", [], supersedes="bridge")],
            requested_status=TaskStatus.COMPLETED, completion_basis_add=["done"]))


def test_supersedes_cycle_cannot_hide_all_completion_claims():
    import pytest
    from applications.coding.state_updates import StatePatch, validate_state_patch, StateValidationError
    from applications.coding.task_state import TaskState, Objective, CompletedItem
    state = TaskState(objective=Objective("repair"), completed=[CompletedItem("a", "a"), CompletedItem("b", "b")])
    with pytest.raises(StateValidationError, match="cycle"):
        validate_state_patch(state, StatePatch(origin="semantic", completed=[
            CompletedItem("a", "a", supersedes="b"), CompletedItem("b", "b", supersedes="a")]))
