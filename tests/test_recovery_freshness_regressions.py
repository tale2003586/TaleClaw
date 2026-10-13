"""Fresh bytes and exact old-source links remain recoverable after state changes."""
from __future__ import annotations

import json

import pytest

from applications.coding.session import TaskSessionFactory
from applications.coding.task_state import ensure_task_state
from evaluation.session_manager import EvaluationSessionManager
from runtime.context import ArtifactStore
from runtime.sessions import Session
from tools.executor import ToolExecutionRequest, ToolExecutor
from tools.handlers import run_read
from tools.hooks import ArtifactAccessGuardHook, ToolLoopGuardHook
from tools.tool_registry import build_lead_tool_registry
from tests.test_evidence_recovery_regressions import _execute


@pytest.mark.parametrize("restored_metadata_cache", [False, True])
def test_changed_file_restores_read_after_same_argument_guard_without_new_snapshot(tmp_path, restored_metadata_cache):
    path = tmp_path / "parser.py"
    path.write_text("old bytes")
    session = Session("freshness:active", metadata={"workspace_root": str(tmp_path)})
    executor = ToolExecutor([ToolLoopGuardHook(repeat_limit=3)])
    def read():
        return executor.execute(ToolExecutionRequest("read", "read_file", {"path": "parser.py", "offset": 0, "limit": None},
             session_id=session.id, metadata=session.metadata, session=session),
             lambda name, args: run_read(**args, _session=session))
    assert read().status == "success"
    if restored_metadata_cache:
        # A restarted hook must validate the durable cache too.
        executor.hooks = [ToolLoopGuardHook(repeat_limit=3)]
    assert read().status == "success"
    assert read().status == "denied"
    path.write_text("new collaborator bytes")
    refreshed = read()
    assert refreshed.status == "success", refreshed.output
    assert "new collaborator bytes" in refreshed.output and "old bytes" not in refreshed.output
    assert read().status == "success"
    assert read().status == "denied", "unchanged same-generation repeats still hit the guard"


def test_old_raw_source_links_recover_only_local_clones_across_two_branches(tmp_path):
    manager = EvaluationSessionManager()
    factory = TaskSessionFactory(manager, root=tmp_path / "tasks")
    source = factory.create(parent_session_id="parent", task_type="coding", user_request="inspect", user_id="alice")
    session = source.session
    ensure_task_state(session, objective_summary="inspect")
    session.add_message("user", "Exact original task requirements")
    event_ref = "event://" + session.event_log[0].event_id
    session.context_snapshots.append({"snapshot_id": "original-snapshot", "session_id": session.id,
        "summary": "unverified original observation", "covered_event_start_seq": 1,
        "covered_event_end_seq": 1, "source_task_state_version": 1})
    snapshot_ref = "snapshot://original-snapshot"
    session.add_message("assistant", "Recover from " + event_ref + " and " + snapshot_ref)
    manager.save(session)
    first = factory.branch(source)
    second = factory.branch(first)
    registry = build_lead_tool_registry(artifact_store=ArtifactStore(tmp_path / "artifacts"))
    executor = ToolExecutor([ArtifactAccessGuardHook()])
    for branch in (first.session, second.session):
        assert branch.messages[-1]["content"] == session.messages[-1]["content"]
        for reference in (event_ref, snapshot_ref):
            restored = _execute(registry, executor, branch, {"artifact_ref": reference, "limit": 50_000})
            assert restored.status == "success", restored.output
            header, body = restored.output.split("\n\n", 1)
            header, body = json.loads(header), json.loads(body)
            assert header["artifact_ref"] == reference
            assert header["resolved_artifact_ref"] != reference
            assert header["session_id"] == body["session_id"] == branch.id
            if reference == snapshot_ref:
                assert header["origin"] == "unverified_summary"
            else:
                assert body["payload"]["message"]["content"] == "Exact original task requirements"
    assert session.metadata.get("branch_lineage") is None


def test_forged_branch_alias_never_grants_foreign_session_access(tmp_path):
    parent = Session("private")
    parent.add_message("user", "foreign secret")
    child = Session("local", metadata={"branch_lineage": {
        "event_id_map": {"claimed-event": parent.event_log[0].event_id},
        "snapshot_id_map": {"claimed-snapshot": "private-snapshot"}}})
    # Even a foreign object erroneously present in a local collection is not authority.
    child.event_log.append(parent.event_log[0])
    child.context_snapshots.append({"session_id": parent.id, "snapshot_id": "private-snapshot", "summary": "foreign secret"})
    registry = build_lead_tool_registry(artifact_store=ArtifactStore(tmp_path / "artifacts"))
    executor = ToolExecutor([ArtifactAccessGuardHook()])
    for reference in ("event://claimed-event", "snapshot://claimed-snapshot"):
        result = _execute(registry, executor, child, {"artifact_ref": reference})
        assert result.status == "error" and "not found in the current session" in result.output
        assert "foreign secret" not in result.output


def _verification_session(tmp_path):
    from applications.coding.task_state import EvidenceRef, save_task_state
    from hashlib import sha256
    path = tmp_path / "parser.py"
    path.write_text("first version")
    session = Session("freshness:verification", metadata={"workspace_root": str(tmp_path)})
    state = ensure_task_state(session, objective_summary="verify parser")
    state.evidence_index["read"] = EvidenceRef("read", "read-event", provenance="host_tool_result",
        tool_name="read_file", workspace_root=str(tmp_path),
        file_dependencies={"parser.py": sha256(path.read_bytes()).hexdigest()})
    save_task_state(session, state)
    return session, path


def _record_verification(session, event_id, result):
    from applications.coding.state_updates import DeterministicEventExtractor, reduce_task_state
    from applications.coding.task_state import load_task_state, save_task_state
    patch = DeterministicEventExtractor().extract([{"event_id": event_id, "event_type": "tool_result", "payload": {
        "tool_name": "bash", "arguments": {"command": "pytest"}, "content": str(result),
        "metadata": result.metadata, "effect_state": result.effect_state}}])
    state = reduce_task_state(load_task_state(session), patch)
    save_task_state(session, state)
    return state.evidence_index["evidence:" + event_id]


def test_fresh_verification_binds_current_known_files_and_invalidates_on_second_external_edit(tmp_path, monkeypatch):
    from hashlib import sha256
    from applications.coding.evidence_validity import refresh_evidence_validity
    from applications.coding.task_state import load_task_state, save_task_state
    from tools.executor import ToolHandlerOutput
    from tools.handlers import run_bash
    session, path = _verification_session(tmp_path)
    path.write_text("second version")
    state = refresh_evidence_validity(load_task_state(session), tmp_path)
    assert state.evidence_index["read"].stale
    save_task_state(session, state)
    monkeypatch.setattr("tools.command.run_command", lambda *a, **kw: ToolHandlerOutput(
        "tests passed", effect_state="no_effect"))
    first = _record_verification(session, "verify-second", run_bash("pytest", _session=session))
    assert first.verification and not first.stale
    assert first.file_dependencies == {"parser.py": sha256(b"second version").hexdigest()}
    # A subsequent build with no edit keeps the new verification valid.
    unchanged = refresh_evidence_validity(load_task_state(session), tmp_path)
    assert not unchanged.evidence_index[first.id].stale
    path.write_text("third version")
    state = refresh_evidence_validity(unchanged, tmp_path)
    assert state.evidence_index[first.id].stale
    save_task_state(session, state)
    recovered = _record_verification(session, "verify-third", run_bash("pytest", _session=session))
    assert not recovered.stale
    assert recovered.file_dependencies == {"parser.py": sha256(b"third version").hexdigest()}
    assert not refresh_evidence_validity(load_task_state(session), tmp_path).evidence_index[recovered.id].stale


@pytest.mark.parametrize("disruption", ["changed", "missing"])
def test_files_changed_or_unavailable_during_verification_require_a_rerun(tmp_path, monkeypatch, disruption):
    from tools.executor import ToolHandlerOutput
    from tools.handlers import run_bash
    session, path = _verification_session(tmp_path)
    def execute(*args, **kwargs):
        if disruption == "changed":
            path.write_text("rewritten while tests ran")
        else:
            path.unlink()
        return ToolHandlerOutput("tests passed", effect_state="unknown")
    monkeypatch.setattr("tools.command.run_command", execute)
    evidence = _record_verification(session, "racing-test", run_bash("pytest", _session=session))
    assert evidence.verification and evidence.stale
    assert "during verification" in evidence.stale_reason


def test_recent_uncompacted_tool_observation_is_bound_but_unobserved_files_are_outside_scope(tmp_path, monkeypatch):
    from hashlib import sha256
    from applications.coding.evidence_validity import refresh_evidence_validity
    from applications.coding.task_state import load_task_state
    from tools.executor import ToolHandlerOutput
    from tools.handlers import run_bash
    observed, unobserved = tmp_path / "observed.py", tmp_path / "unobserved.py"
    observed.write_text("observed source")
    unobserved.write_text("outside capture scope")
    session = Session("freshness:recent", metadata={"workspace_root": str(tmp_path)})
    ensure_task_state(session, objective_summary="verify")
    content = run_read("observed.py", _session=session)
    session.add_message("tool", str(content), tool_call_id="read", metadata=content.metadata)
    assert not load_task_state(session).evidence_index
    monkeypatch.setattr("tools.command.run_command", lambda *a, **kw: ToolHandlerOutput("tests passed", effect_state="no_effect"))
    evidence = _record_verification(session, "recent-test", run_bash("pytest", _session=session))
    assert evidence.file_dependencies == {"observed.py": sha256(observed.read_bytes()).hexdigest()}
    unobserved.write_text("external edit outside declared scope")
    assert not refresh_evidence_validity(load_task_state(session), tmp_path).evidence_index[evidence.id].stale


def test_verification_after_intentional_deletion_stays_valid_until_path_reappears(tmp_path, monkeypatch):
    from applications.coding.evidence_validity import FILE_ABSENT_VERSION, refresh_evidence_validity
    from applications.coding.task_state import load_task_state, save_task_state
    from tools.executor import ToolHandlerOutput
    from tools.handlers import run_bash
    session, path = _verification_session(tmp_path)
    path.unlink()
    save_task_state(session, refresh_evidence_validity(load_task_state(session), tmp_path))
    monkeypatch.setattr("tools.command.run_command", lambda *a, **kw: ToolHandlerOutput("tests passed", effect_state="no_effect"))
    evidence = _record_verification(session, "after-delete", run_bash("pytest", _session=session))
    assert evidence.file_dependencies == {"parser.py": FILE_ABSENT_VERSION}
    assert not evidence.stale
    assert not refresh_evidence_validity(load_task_state(session), tmp_path).evidence_index[evidence.id].stale
    path.write_text("recreated unexpectedly")
    assert refresh_evidence_validity(load_task_state(session), tmp_path).evidence_index[evidence.id].stale


def test_real_shell_result_carries_host_file_binding_through_extraction(tmp_path):
    from hashlib import sha256
    import shlex
    import sys
    from tools.handlers import run_bash
    session, path = _verification_session(tmp_path)
    command = shlex.quote(sys.executable) + " -c " + shlex.quote("print('tests passed')")
    result = run_bash(command, _session=session)
    assert result.status == "success", result
    assert result.exit_code == 0
    evidence = _record_verification(session, "real-process", result)
    assert evidence.verification and not evidence.stale
    assert evidence.file_dependencies == {"parser.py": sha256(path.read_bytes()).hexdigest()}
