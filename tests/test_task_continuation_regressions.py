"""Native task lifecycle contracts across model calls, forks, and storage restarts."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from types import SimpleNamespace

import pytest

from applications.coding.context_state import _compact_events
from applications.coding.handoff import CODING_HANDOFF_METADATA_KEY
from applications.coding.session import TaskSessionFactory
from applications.coding.task_lifecycle import ACTIVE_CODING_TASK_ID
from applications.coding.task_state import (
    CompletedItem, Constraint, EvidenceRef, ItemStatus, load_task_state, save_task_state,
)
from applications.turn_coordinator import TurnCoordinator
from runtime.context import ContextBuilder
from runtime.context.events import ContextEventType
from runtime.context.snapshots import EventCompactor
from runtime.sessions import Session, SessionManager
from runtime.workspace import WorkspaceResolver
from tests.fakes import make_agent_spec
from tests.test_long_request_externalization import _direct_coding_runner

SPEC = make_agent_spec("coding", "coding", "coding")


def start(tmp_path):
    runner, sessions = _direct_coding_runner(tmp_path)
    parent = Session("web:native", active_agent="coding", metadata={"user_id": "alice"})
    runner.run_coding_task(parent_session=parent, user_text="Implement parser; preserve API v1.", agent_spec=SPEC)
    return runner, sessions, parent, active(sessions, parent)


def active(sessions, parent):
    return sessions.sessions["task:" + parent.metadata[ACTIVE_CODING_TASK_ID]]


def compact(task):
    result = _compact_events(task, task_state=load_task_state(task),
                             before_message_index=len(task.messages), event_compactor=EventCompactor())
    assert result is not None
    return result[1]


def test_native_continuation_keeps_task_identity_state_snapshots_and_exact_sources(tmp_path):
    runner, sessions, parent, task = start(tmp_path)
    state = load_task_state(task)
    state.constraints.append(Constraint("api", "Preserve API v1."))
    save_task_state(task, state)
    snapshot = compact(task)
    original_ids = [event.event_id for event in task.event_log]
    original_task_id = task.metadata["task_id"]
    parent.metadata["context_policy"] = {"name": "masking", "preview_chars": 50}
    runner.run_coding_task(parent_session=parent, user_text="Inspect the failing edge case.", agent_spec=SPEC)
    resumed = active(sessions, parent)
    assert resumed is task
    assert resumed.metadata["task_id"] == original_task_id
    assert resumed.metadata["last_task_mode"] == "continue"
    assert resumed.metadata["task_turn_count"] == 2
    assert resumed.active_snapshot_id == snapshot.snapshot_id
    assert [event.event_id for event in resumed.event_log[:len(original_ids)]] == original_ids
    assert load_task_state(resumed).constraints[0].id == "api"
    assert resumed.metadata["context_policy"] == parent.metadata["context_policy"]
    assert resumed.metadata["context_policy"] is not parent.metadata["context_policy"]
    prompt = json.dumps(runner.base_runtime.agent_runner.provider.calls[-1]["messages"])
    assert "Preserve API v1." in prompt and "Inspect the failing edge case." in prompt


def test_explicit_new_task_does_not_inherit_earlier_task_constraints_or_requests(tmp_path):
    runner, sessions, parent, old = start(tmp_path)
    state = load_task_state(old)
    state.constraints.append(Constraint("private", "OLD_TASK_ONLY_REQUIREMENT"))
    save_task_state(old, state)
    before = deepcopy(old)
    parent.add_message("assistant", "The original task is complete.")
    parent.metadata["context_policy"] = "adaptive"
    runner.run_coding_task(parent_session=parent, user_text="Build an unrelated formatter.", agent_spec=SPEC,
                           task_mode="new")
    new = active(sessions, parent)
    assert new.id != old.id
    assert old.messages == before.messages and old.metadata == before.metadata
    assert not load_task_state(new).constraints
    assert new.metadata[CODING_HANDOFF_METADATA_KEY]["source_requests"] == []
    assert new.metadata["context_policy"] == "adaptive"
    prompt = json.dumps(runner.base_runtime.agent_runner.provider.calls[-1]["messages"])
    assert "OLD_TASK_ONLY_REQUIREMENT" not in prompt
    assert "Implement parser; preserve API v1." not in prompt


def test_continue_selects_explicit_owned_task_after_another_task_became_active(tmp_path):
    runner, sessions, parent, original = start(tmp_path)
    runner.run_coding_task(parent_session=parent, user_text="Unrelated task", agent_spec=SPEC, task_mode="new")
    second = active(sessions, parent)
    runner.run_coding_task(parent_session=parent, user_text="Inspect the parser again", agent_spec=SPEC,
                           task_mode="continue", task_id=original.metadata["task_id"])
    assert active(sessions, parent) is original
    assert original.metadata["task_turn_count"] == 2
    assert second.metadata["task_turn_count"] == 1


def test_branch_rekeys_recovery_lineage_and_mutates_neither_parent_state_nor_side_effects(tmp_path):
    runner, sessions, parent, original = start(tmp_path)
    original.add_message("assistant", None, tool_calls=[{"id": "historical-write", "function": {
        "name": "write_file", "arguments": '{"path":"marker.txt","content":"once"}',
    }}])
    original.add_message("tool", "historical side effect succeeded", tool_call_id="historical-write")
    marker = tmp_path / "marker.txt"
    marker.write_text("once")
    state = load_task_state(original)
    state.evidence_index["write"] = EvidenceRef("write", original.event_log[-1].event_id,
                                               verification=True, task_id=state.task_id)
    save_task_state(original, state)
    prior = compact(original)
    original_before = deepcopy(original)
    original_event_log = list(original.event_log)
    runner.run_coding_task(parent_session=parent, user_text="Explore an alternative parser design.",
                           agent_spec=SPEC, task_mode="branch")
    branch = active(sessions, parent)
    assert branch.id != original.id
    assert original.messages == original_before.messages
    assert original.metadata == original_before.metadata
    assert original.event_log == original_event_log
    assert original.context_snapshots == original_before.context_snapshots
    assert marker.read_text() == "once"
    assert branch.metadata["branch_lineage"]["task_id"] == original.metadata["task_id"]
    assert branch.metadata["branch_lineage"]["snapshot_id"] == prior.snapshot_id
    assert branch.active_snapshot_id != prior.snapshot_id
    assert not {event.event_id for event in original.event_log} & {event.event_id for event in branch.event_log}
    assert all(event.session_id == branch.id for event in branch.event_log)
    assert all(snapshot["session_id"] == branch.id for snapshot in branch.context_snapshots)
    assert load_task_state(branch).evidence_index["write"].stale
    assert not load_task_state(original).evidence_index["write"].stale
    assert branch.messages[0]["content"] == original.messages[0]["content"]
    assert sum(event.type == "tool_result" for event in branch.event_log) == 1


def test_explicit_requirement_revision_changes_active_facts_and_is_an_immutable_user_event(tmp_path):
    runner, sessions, parent, task = start(tmp_path)
    state = load_task_state(task)
    state.constraints.extend([Constraint("api", "Preserve API v1."), Constraint("db", "Do not modify the database.")])
    save_task_state(task, state)
    objective = deepcopy(state.objective)
    revision = {"constraints": [{"id": "api", "text": "Use API v2."}], "remove_constraint_ids": ["db"]}
    runner.run_coding_task(parent_session=parent, user_text="API v2 is approved; the database restriction is withdrawn.",
                           agent_spec=SPEC, task_mode="continue", requirements_revision=revision)
    state = load_task_state(task)
    assert active(sessions, parent) is task
    assert state.objective == objective
    assert state.constraints[0].text == "Use API v2."
    assert state.constraints[1].status == ItemStatus.SUPERSEDED
    assert state.constraints[1].required is False
    correction = next(event for event in task.event_log if event.type == ContextEventType.USER_CORRECTION.value)
    assert correction.payload["message"]["metadata"]["requirements_revision"]["remove_constraint_ids"] == ("db",)
    assert state.history[-1].source_patch_id == f"user-revision:event://{correction.event_id}"
    assert task.metadata["requirements_revision_events"] == [f"event://{correction.event_id}"]
    assert revision == {"constraints": [{"id": "api", "text": "Use API v2."}], "remove_constraint_ids": ["db"]}


@pytest.mark.parametrize("kind", ["foreign_parent", "foreign_user", "missing", "path_traversal", "workspace_swap", "unknown_revision"])
def test_invalid_target_or_revision_fails_before_new_task_or_model_call(tmp_path, kind):
    runner, sessions, parent, task = start(tmp_path)
    runner.workspace_resolver = WorkspaceResolver(allowed_roots=[tmp_path], default_workspace=tmp_path)
    kwargs = dict(parent_session=parent, user_text="Follow-up", agent_spec=SPEC,
                  task_mode="continue", task_id=task.metadata["task_id"])
    if kind == "foreign_parent":
        kwargs["parent_session"] = Session("web:other", metadata={"user_id": "alice"})
    elif kind == "foreign_user":
        kwargs["parent_session"] = Session(parent.id, metadata={"user_id": "bob"})
    elif kind == "missing":
        kwargs["task_id"] = "coding-missing"
    elif kind == "path_traversal":
        kwargs["task_id"] = "../foreign"
    elif kind == "workspace_swap":
        other = tmp_path / "other"
        other.mkdir()
        kwargs["workspace_root"] = other
    else:
        kwargs["requirements_revision"] = {"remove_constraint_ids": ["unknown"]}
    prior_calls = len(runner.base_runtime.agent_runner.provider.calls)
    prior_sessions = set(sessions.sessions)
    with pytest.raises(ValueError):
        runner.run_coding_task(**kwargs)
    assert len(runner.base_runtime.agent_runner.provider.calls) == prior_calls
    assert set(sessions.sessions) == prior_sessions


def test_turn_coordinator_consumes_task_selector_only_for_coding_route():
    calls = []
    coding = SimpleNamespace(run_coding_task=lambda **kwargs: calls.append(kwargs) or "done")
    coordinator = TurnCoordinator(None, None, None, None, coding_application=coding)
    parent = Session("web:transport")
    selector = {"mode": "branch", "task_id": "coding-1234"}
    inbound = SimpleNamespace(content="Explore another solution", metadata={"coding_task": selector})
    coordinator._execute(parent, inbound, SimpleNamespace(agent_spec=SPEC), SimpleNamespace(run_id="one"), None)
    assert calls[0]["task_mode"] == "branch" and calls[0]["task_id"] == "coding-1234"
    generic_calls = []
    coordinator.runtime = SimpleNamespace(run=lambda *args: generic_calls.append(args) or SimpleNamespace(output="generic"))
    coordinator._execute(parent, inbound, SimpleNamespace(agent_spec=make_agent_spec("bot", "bot", "bot")),
                         SimpleNamespace(run_id="two"), None)
    assert len(calls) == 1 and len(generic_calls) == 1


def test_web_chat_endpoint_forwards_explicit_task_selector(monkeypatch):
    from web.server import RequestHandler
    selector = {"mode": "continue", "task_id": "coding-1234", "requirements_revision": {"objective": "Inspect lexer"}}
    handler = object.__new__(RequestHandler)
    handler.headers = {"Content-Type": "application/json", "Host": "localhost:8000"}
    handler._read_json_body = lambda: {"message": "Inspect lexer", "coding_task": selector}
    handler._current_user = lambda: SimpleNamespace(user_id="alice", role="admin")
    calls, responses = [], []
    run = {"run_id": "run_selector", "session_id": "default", "status": "completed"}
    handler.agent_service = SimpleNamespace(
        submit=lambda **kwargs: calls.append(kwargs) or run,
        get_run=lambda *_args, **_kwargs: run,
        events_after=lambda *_args, **_kwargs: [{"type": "complete", "seq": 1, "reply": "done"}],
    )
    handler._send_json = lambda payload, **kwargs: responses.append(payload)
    monkeypatch.setattr("web.server.read_session", lambda *_args, **_kwargs: {})
    handler._handle_chat()
    assert calls[0]["coding_task"] == selector
    assert responses[0]["reply"] == "done"


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Explicit isolated PostgreSQL DATABASE_URL required")
def test_postgres_restart_restores_native_task_and_branch_snapshots_atomically(tmp_path):
    from tests.postgres_utils import temporary_postgres_schema
    with temporary_postgres_schema("native_task_lifecycle") as dsn:
        manager = SessionManager(dsn)
        runner, _ = _direct_coding_runner(tmp_path)
        runner.sessions = manager
        runner.factory = TaskSessionFactory(manager, root=tmp_path / "tasks")
        parent = manager.get_or_create("web:persistent")
        parent.metadata["user_id"] = "alice"
        runner.run_coding_task(parent_session=parent, user_text="Inspect parser", agent_spec=SPEC)
        task_id = parent.metadata[ACTIVE_CODING_TASK_ID]
        original = manager.get_existing("task:" + task_id)
        state = load_task_state(original)
        state.constraints.append(Constraint("api", "Preserve API v1."))
        save_task_state(original, state)
        manager.compact(original, checkpoint={"task_state": state.to_dict()}, archive_boundary_seq=1)
        snapshot = compact(original)
        manager.save(original)
        old_ids = {event.event_id for event in original.event_log}
        manager.close()

        manager = SessionManager(dsn)
        runner.sessions = manager
        runner.factory = TaskSessionFactory(manager, root=tmp_path / "tasks")
        parent = manager.get_existing("web:persistent")
        runner.run_coding_task(parent_session=parent, user_text="Recheck edge cases", agent_spec=SPEC)
        resumed = manager.get_existing("task:" + task_id)
        assert parent.metadata[ACTIVE_CODING_TASK_ID] == task_id
        assert resumed.active_snapshot_id == snapshot.snapshot_id
        assert old_ids <= {event.event_id for event in resumed.event_log}
        assert load_task_state(resumed).constraints[0].id == "api"
        runner.run_coding_task(parent_session=parent, user_text="Try another design", agent_spec=SPEC, task_mode="branch")
        branch_id = parent.metadata[ACTIVE_CODING_TASK_ID]
        branch = manager.get_existing("task:" + branch_id)
        branch_snapshot = branch.active_snapshot_id
        branch_checkpoint = branch.checkpoints[0]["checkpoint_id"]
        manager.close()

        manager = SessionManager(dsn)
        try:
            parent = manager.get_existing("web:persistent")
            branch = manager.get_existing("task:" + branch_id)
            original = manager.get_existing("task:" + task_id)
            assert parent.metadata[ACTIVE_CODING_TASK_ID] == branch_id
            assert branch.active_snapshot_id == branch_snapshot != original.active_snapshot_id
            assert branch.checkpoints[0]["checkpoint_id"] == branch_checkpoint != original.checkpoints[0]["checkpoint_id"]
            assert all(event.session_id == branch.id for event in branch.event_log)
            assert not {event.event_id for event in branch.event_log} & {event.event_id for event in original.event_log}
            before = deepcopy(original)
            with pytest.raises(ValueError, match="already exists"):
                manager.save_branch(branch)
            assert manager.get_existing(original.id).messages == before.messages
            failed = Session("task:failed-branch", messages=[{"role": "user", "content": "must roll back"}])
            failed.context_snapshots = [{"session_id": "foreign-source"}]
            with pytest.raises(ValueError, match="another session"):
                manager.save_branch(failed)
            assert manager.get_existing(failed.id) is None
            assert manager.get_existing(original.id).messages == before.messages
        finally:
            manager.close()


def test_objective_revision_keeps_task_identity_and_records_previous_requirement(tmp_path):
    runner, sessions, parent, task = start(tmp_path)
    old_objective = deepcopy(load_task_state(task).objective)
    runner.run_coding_task(parent_session=parent, user_text="Change the goal to lexer diagnostics.",
                           agent_spec=SPEC, requirements_revision={"objective": "Inspect lexer diagnostics"})
    state = load_task_state(task)
    assert active(sessions, parent) is task
    assert state.objective.summary == "Inspect lexer diagnostics"
    assert state.objective.supersedes == old_objective.original_request_ref
    assert state.objective.original_request_ref == task.metadata["requirements_revision_events"][-1]
    assert any(entry.category == "objective" and entry.previous["summary"] == old_objective.summary
               for entry in state.history)


def test_branch_first_durable_save_and_checkpoint_recovery_invalidate_inherited_proof(tmp_path, monkeypatch):
    runner, sessions, parent, original = start(tmp_path)
    state = load_task_state(original)
    state.evidence_index["old-proof"] = EvidenceRef("old-proof", "old-event", verification=True)
    state.completed.append(CompletedItem("done", "parser fixed", ["old-proof"]))
    save_task_state(original, state)
    original.checkpoints.append({"checkpoint_id": "old-checkpoint", "state": {"task_state": state.to_dict()}})
    original.metadata["native_response_context"] = {"lineage": {"session_id": original.id},
                                                     "input_items": [{"type": "compaction", "encrypted_content": "opaque"}]}
    original.metadata["context_compaction_request"] = {"mode": "native"}
    original.metadata["resource_state"] = {"artifact_prompt_visibility": {"old-proof": True}}
    source = runner.factory.load(original.metadata["task_id"], parent_session_id=parent.id, user_id="alice")
    saved = []
    original_save = sessions.save
    def inspect_first_save(session):
        saved.append(deepcopy(session))
        original_save(session)
    monkeypatch.setattr(sessions, "save", inspect_first_save)
    branch = runner.factory.branch(source).session
    first = saved[0]
    assert load_task_state(first).evidence_index["old-proof"].stale
    assert load_task_state(first).completed[0].needs_parent_verification
    assert load_task_state(first).task_id == first.metadata["task_id"]
    assert "native_response_context" not in first.metadata
    assert "context_compaction_request" not in first.metadata
    assert "resource_state" not in first.metadata
    first.metadata.pop("task_state")
    recovered = load_task_state(first)
    assert recovered.evidence_index["old-proof"].stale
    assert recovered.completed[0].needs_parent_verification
    # A process stopping immediately after factory.branch leaves safe durable state;
    # the explicitly selected branch can subsequently resume without replay.
    runner.run_coding_task(parent_session=parent, user_text="Resume branch after interruption", agent_spec=SPEC,
                           task_mode="continue", task_id=branch.metadata["task_id"])
    assert active(sessions, parent) is branch
    assert load_task_state(branch).completed[0].needs_parent_verification
    assert not load_task_state(original).evidence_index["old-proof"].stale


@pytest.mark.parametrize("operation", ["revision", "branch"])
def test_revised_or_branched_task_can_complete_after_new_verification(tmp_path, operation):
    from applications.coding.task_lifecycle import apply_requirements_revision
    from applications.coding.state_updates import StatePatch, reduce_task_state
    from runtime.task_state.models import TaskStatus
    runner, sessions, parent, task = start(tmp_path)
    state = load_task_state(task)
    state.evidence_index["old-pass"] = EvidenceRef("old-pass", "test-before-revision", verification=True,
                                                  tool_name="bash", provenance="host_tool_result")
    state.completed = [CompletedItem("done", "parser fixed", ["old-pass"])]
    state.completion_basis = ["old-pass"]
    state.status = TaskStatus.COMPLETED
    save_task_state(task, state)
    if operation == "revision":
        apply_requirements_revision(task, {"objective": "Fix lexer instead"}, "event://revision")
    else:
        source = runner.factory.load(task.metadata["task_id"], parent_session_id=parent.id, user_id="alice")
        task = runner.factory.branch(source).session
    state = load_task_state(task)
    assert not state.completion_basis
    assert any(item.category == "completion_basis" and item.previous["refs"] == ["old-pass"]
               for item in state.history)
    assert state.completed[0].needs_parent_verification
    fresh = EvidenceRef("new-pass", "test-after-revision", verification=True, tool_name="bash",
                        provenance="host_tool_result", task_id=state.task_id)
    state = reduce_task_state(state, StatePatch(evidence=[fresh]))
    state = reduce_task_state(state, StatePatch(completed=[CompletedItem(
        "done", "revised task verified", ["new-pass"], supersedes="done")]))
    state = reduce_task_state(state, StatePatch(requested_status=TaskStatus.COMPLETED,
                                               completion_basis_add=["new-pass"]))
    assert state.status == TaskStatus.COMPLETED
    assert not state.completed[0].needs_parent_verification


def test_evaluation_task_store_supports_native_branches_without_aliasing_nested_state():
    from evaluation.session_manager import EvaluationSessionManager
    manager = EvaluationSessionManager()
    assert manager.get_existing("missing") is None
    source = Session("task:source", metadata={"nested": {"items": [1]}})
    manager.save(source)
    branch = Session("task:branch", metadata=deepcopy(source.metadata))
    manager.save_branch(branch)
    branch.metadata["nested"]["items"].append(2)
    assert manager.get_existing(branch.id).metadata["nested"]["items"] == [1]
    assert manager.get_existing(source.id).metadata["nested"]["items"] == [1]
    with pytest.raises(ValueError, match="already exists"):
        manager.save_branch(branch)


@pytest.mark.parametrize("mode", ["continue", "branch"])
def test_resumed_tasks_use_current_parent_authorization_role(tmp_path, mode):
    runner, sessions, parent, original = start(tmp_path)
    assert original.metadata["user_role"] == "admin"
    parent.metadata["user_role"] = "user"
    runner.run_coding_task(parent_session=parent, user_text="Inspect again", agent_spec=SPEC, task_mode=mode)
    assert active(sessions, parent).metadata["user_role"] == "user"


def test_branch_preserves_structured_user_content_as_exact_source(tmp_path):
    runner, sessions, parent, task = start(tmp_path)
    source_ref = "event://" + task.event_log[0].event_id
    content = [{"type": "text", "text": source_ref}, {"type": "image", "source": {"url": "snapshot://old"}}]
    task.add_message("user", content)
    source = runner.factory.load(task.metadata["task_id"], parent_session_id=parent.id, user_id="alice")
    branch = runner.factory.branch(source).session
    assert branch.messages[-1]["content"] == content
    assert branch.messages[-1]["content"] is not content
    mapped_id = branch.metadata["branch_lineage"]["event_id_map"][task.event_log[-1].event_id]
    cloned = next(event for event in branch.event_log if event.event_id == mapped_id)
    assert cloned.payload["message"]["content"] == task.event_log[-1].payload["message"]["content"]
