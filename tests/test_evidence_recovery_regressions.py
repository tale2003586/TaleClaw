from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.subagent.runner import TaskSubagentRunner
from runtime.context import ArtifactStore
from runtime.context.artifact_access import (
    load_artifact_access_states,
    update_artifact_prompt_visibility,
)
from runtime.context.builder import ContextBuilder
from runtime.sessions import Session
from tools.executor import ToolExecutionRequest, ToolExecutor
from tools.hooks import ArtifactAccessGuardHook, ToolLoopGuardHook
from tools.tool_registry import build_lead_tool_registry, build_teammate_tool_registry


def _execute(registry, executor, session, arguments, *, mode="bot", agent_spec=None):
    registry.execute("tool_search", {"query": "read_artifact"}, session=session,
                     mode=mode, agent_spec=agent_spec)
    return executor.execute(
        ToolExecutionRequest("recovery", "read_artifact", arguments,
                             session_id=session.id, metadata=session.metadata, session=session),
        lambda name, args: registry.execute(name, args, session=session, mode=mode,
                                           agent_spec=agent_spec),
    )


def _tool_message(result):
    return {"role": "tool", "tool_call_id": "recovery", "content": result.output,
            "metadata": result.metadata}


def test_dropped_and_truncated_evidence_can_be_rehydrated_repeatedly(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_artifact("REQUIRED middle evidence " * 100)
    registry = build_lead_tool_registry(artifact_store=store)
    executor = ToolExecutor([ArtifactAccessGuardHook(), ToolLoopGuardHook()])
    session = Session("recovery:bounded")
    args = {"artifact_ref": ref.storage_uri, "limit": 200}
    update_artifact_prompt_visibility(session.metadata, [])
    for iteration in range(8):
        result = _execute(registry, executor, session, args)
        assert result.status == "success", (iteration, result.output)
        assert "REQUIRED middle evidence" in result.output
        full = _tool_message(result)
        update_artifact_prompt_visibility(session.metadata, [full])
        duplicate = _execute(registry, executor, session, args)
        assert duplicate.status == "denied"
        # Host metadata survives trimming: a digest prevents false visibility.
        update_artifact_prompt_visibility(session.metadata, [
            {**full, "content": full["content"][:80] + "[truncated]"},
        ])
    state = load_artifact_access_states(session.metadata)[ref.storage_uri]
    assert state.covered_ranges == [(0, 200)]
    assert state.total_read_calls == 8
    assert state.no_progress_count == 1  # Only the most recent visible duplicate.


def test_builder_uses_final_prompt_to_release_old_coverage(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_artifact("old immutable evidence " * 20)
    registry = build_lead_tool_registry(artifact_store=store)
    executor = ToolExecutor([ArtifactAccessGuardHook()])
    session = Session("recovery:actual-builder")
    first = _execute(registry, executor, session, {"artifact_ref": ref.storage_uri})
    assert first.status == "success"
    session.add_message("user", "Continue the task; the old tool output was compacted.")
    bundle = ContextBuilder().build(
        session=session, agent_spec=SimpleNamespace(instructions="base", tool_mode="bot"),
    )
    assert not any(message.get("content") == first.output for message in bundle.messages)
    restored = _execute(registry, executor, session, {"artifact_ref": ref.storage_uri})
    assert restored.status == "success"
    assert restored.metadata["artifact_access"]["rehydrated"] is True


def test_exact_search_whitespace_and_larger_result_window_are_not_duplicates(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_artifact("TARGET one\nTARGET  one\nTARGET one\n")
    registry = build_lead_tool_registry(artifact_store=store)
    executor = ToolExecutor([ArtifactAccessGuardHook()])
    session = Session("recovery:query")
    update_artifact_prompt_visibility(session.metadata, [])
    for query, max_results, count in [("TARGET one", 1, 1), ("TARGET  one", 1, 1),
                                       ("TARGET one", 20, 2)]:
        result = _execute(registry, executor, session, {
            "artifact_ref": ref.storage_uri, "query": query, "max_results": max_results,
        })
        assert result.status == "success"
        assert json.loads(result.output)["match_count"] == count


@pytest.mark.parametrize("mode", ["bot", "coding", "teammate", "explore", "code", "plan"])
def test_current_session_event_recovery_is_available_through_real_tool_policy(tmp_path, mode):
    store = ArtifactStore(tmp_path / "artifacts")
    registry = (build_teammate_tool_registry("reader", artifact_store=store)
                if mode == "teammate" else build_lead_tool_registry(artifact_store=store))
    session = Session(f"recovery:{mode}")
    session.add_message("user", "The original hard requirement is 中文结构化输出.")
    event = session.event_log[-1]
    session.set_archive_boundary(event.seq)
    session.messages.clear()
    spec = None
    runtime_mode = mode
    if mode in {"explore", "code", "plan"}:
        spec = object.__new__(TaskSubagentRunner)._agent_spec(mode)
        runtime_mode = "coding"
        assert "retrieve_tool_result" in spec.tool_set.allow
    result = _execute(registry, ToolExecutor([ArtifactAccessGuardHook()]), session,
                      {"artifact_ref": f"event://{event.event_id}"},
                      mode=runtime_mode, agent_spec=spec)
    assert result.status == "success", result.output
    header, body = result.output.split("\n\n", 1)
    assert json.loads(header)["event_type"] == "user_message"
    assert json.loads(header)["instructions"] is False
    assert json.loads(body)["payload"]["message"]["content"].endswith("中文结构化输出.")
    assert result.metadata["artifact_access"]["artifact_ref"] == f"event://{event.event_id}"


def test_event_search_respects_bounds_and_returns_readable_event_references(tmp_path):
    registry = build_lead_tool_registry(artifact_store=ArtifactStore(tmp_path / "artifacts"))
    executor = ToolExecutor([ArtifactAccessGuardHook()])
    session = Session("recovery:archive-search")
    for content in ("needle too old", "needle selected", "needle too new"):
        session.add_message("assistant", content)
    result = _execute(registry, executor, session, {
        "artifact_ref": "event://session", "query": "needle", "start_seq": 2, "end_seq": 2,
    })
    assert result.status == "success"
    matches = json.loads(result.output)["matches"]
    assert len(matches) == 1 and matches[0]["seq"] == 2
    assert matches[0]["event_type"] == "assistant_message"
    recovered = _execute(registry, executor, session, {"artifact_ref": matches[0]["event_ref"]})
    assert "needle selected" in recovered.output
    assert "needle too old" not in recovered.output


def test_recovery_never_follows_a_forged_parent_session_id_or_foreign_snapshot(tmp_path):
    registry = build_lead_tool_registry(artifact_store=ArtifactStore(tmp_path / "artifacts"))
    executor = ToolExecutor([ArtifactAccessGuardHook()])
    parent = Session("private-parent")
    parent.add_message("user", "parent secret")
    child = Session("isolated-child", metadata={"parent_session_id": parent.id})
    child.context_snapshots.append({"session_id": parent.id, "snapshot_id": "foreign",
                                    "summary": "foreign summary"})
    for ref in (f"event://{parent.event_log[-1].event_id}", "snapshot://foreign"):
        result = _execute(registry, executor, child, {"artifact_ref": ref})
        assert result.status == "error"
        assert "not found in the current session" in result.output
        assert "parent secret" not in result.output and "foreign summary" not in result.output


def test_snapshot_recovery_is_paginated_and_retains_unverified_origin(tmp_path):
    registry = build_lead_tool_registry(artifact_store=ArtifactStore(tmp_path / "artifacts"))
    session = Session("recovery:snapshot")
    session.context_snapshots.append({"session_id": session.id, "snapshot_id": "snap_1",
                                      "summary": "model hypothesis " * 300,
                                      "previous_snapshot_id": "snap_0", "generation": 2,
                                      "covered_event_start_seq": 1, "covered_event_end_seq": 100,
                                      "artifact_refs": ["artifact://" + "a" * 20] * 2000,
                                      "working_memory": ["unverified note"]})
    args = {"artifact_ref": "snapshot://snap_1", "limit": 200}
    result = _execute(registry, ToolExecutor([ArtifactAccessGuardHook()]), session, args)
    assert result.status == "success"
    header, body = result.output.split("\n\n", 1)
    parsed = json.loads(header)
    assert parsed["origin"] == "unverified_summary"
    assert parsed["previous_snapshot_ref"] == "snapshot://snap_0"
    assert parsed["generation"] == 2
    assert parsed["covered_event_end_seq"] == 100
    assert parsed["instructions"] is False
    assert parsed["truncated"] is True and parsed["next_offset"] == 200
    assert len(body) == 200


def test_mutable_session_search_can_discover_new_events_after_an_identical_search(tmp_path):
    registry = build_lead_tool_registry(artifact_store=ArtifactStore(tmp_path / "artifacts"))
    executor = ToolExecutor([ArtifactAccessGuardHook(), ToolLoopGuardHook()])
    session = Session("recovery:growing-index")
    args = {"artifact_ref": "event://session", "query": "NEWFACT"}
    before = _execute(registry, executor, session, args)
    # Actual reasoning-loop transport appends tool results directly.
    session.messages.append({"role": "tool", "content": "NEWFACT", "tool_call_id": "new"})
    after = _execute(registry, executor, session, args)
    assert json.loads(before.output)["match_count"] == 0
    assert after.status == "success"
    assert json.loads(after.output)["match_count"] == 1


def test_access_summary_is_bounded_without_losing_durable_history():
    from runtime.context.artifact_access import (
        ArtifactAccessState, artifact_access_summary_message, save_artifact_access_state,
    )

    metadata = {}
    for index in range(100):
        save_artifact_access_state(metadata, ArtifactAccessState(
            artifact_ref=f"artifact://art_{index}",
            covered_ranges=[(offset * 20, offset * 20 + 10) for offset in range(100)],
        ))
    message = artifact_access_summary_message(metadata)
    payload = json.loads(message["content"].split("\n", 1)[1].rsplit("\n", 1)[0])
    assert len(message["content"]) <= 6_000
    assert len(payload["resources"]) <= 20
    assert payload["tracked_resources"] == 100
    assert payload["omitted_resources"] == 100 - len(payload["resources"])
    assert all(len(item["covered_ranges"]) <= 8 for item in payload["resources"])
    assert all(item["omitted_ranges"] == 92 for item in payload["resources"])
    assert len(load_artifact_access_states(metadata)) == 100
    assert len(load_artifact_access_states(metadata)["artifact://art_0"].covered_ranges) == 100


def test_mutable_task_state_recovery_never_reuses_old_version_or_parent_state(tmp_path):
    registry = build_lead_tool_registry(artifact_store=ArtifactStore(tmp_path / "artifacts"))
    executor = ToolExecutor([ArtifactAccessGuardHook(), ToolLoopGuardHook()])
    session = Session("recovery:task-state", metadata={"task_state": {
        "core": {"version": 1, "updated_at": "first", "objective": "current task"},
        "extensions": {"coding": {"evidence_index": {"key": "INITIAL" * 1000}}},
    }})
    args = {"artifact_ref": "state://current", "limit": 200}
    first = _execute(registry, executor, session, args)
    first_header, content = first.output.split("\n\n", 1)
    header = json.loads(first_header)
    assert first.status == "success" and len(content) == 200
    assert header["mutable"] is True and header["instructions"] is False
    assert header["version"] == 1 and header["updated_at"] == "first"
    assert "artifact_access" not in first.metadata
    session.metadata["task_state"]["core"].update(version=2, updated_at="second")
    second = _execute(registry, executor, session, args)
    second_header = json.loads(second.output.split("\n\n", 1)[0])
    assert second.status == "success" and second_header["version"] == 2
    assert second_header["state_sha256"] != header["state_sha256"]
    found = _execute(registry, executor, session, {"artifact_ref": "state://current", "query": "INITIAL"})
    assert found.status == "success" and json.loads(found.output)["match_count"] > 0
    child = Session("recovery:isolated-state", metadata={"parent_session_id": session.id})
    missing = _execute(registry, executor, child, args)
    assert missing.status == "error" and "No task state exists" in missing.output
    forbidden = _execute(registry, executor, child, {"artifact_ref": "state://recovery:task-state"})
    assert forbidden.status == "error" and "Only state://current" in forbidden.output


def test_real_compactor_cold_model_notes_are_recoverable_from_current_snapshot(tmp_path):
    from applications.coding.context_state import _compact_events
    from applications.coding.task_state import Constraint, Objective, TaskState, save_task_state
    from runtime.context.snapshots import CompactionLimits, EventCompactor

    session = Session("recovery:real-compactor")
    session.add_message("user", "Debug parser. Preserve API v1.")
    state = TaskState(objective=Objective("Debug parser", "event://" + session.event_log[0].event_id),
                      constraints=[Constraint("api", "Preserve API v1.")])
    save_task_state(session, state)
    notes = "Global objective: Debug parser\nConstraints:\n- Preserve API v1.\nHypothesis: COLD_MODEL_NOTE_MARKER"
    provider = SimpleNamespace(chat=lambda **kwargs: SimpleNamespace(content=notes))
    result = _compact_events(
        session, task_state=state, before_message_index=len(session.messages),
        event_compactor=EventCompactor(provider=provider, limits=CompactionLimits(working_memory_max_chars=0)),
    )
    assert result is not None
    snapshot = result[1]
    assert "COLD_MODEL_NOTE_MARKER" not in snapshot.summary
    assert snapshot.model_notes == notes
    registry = build_lead_tool_registry(artifact_store=ArtifactStore(tmp_path / "artifacts"))
    recovered = _execute(registry, ToolExecutor([ArtifactAccessGuardHook()]), session,
                         {"artifact_ref": f"snapshot://{snapshot.snapshot_id}", "limit": 50_000})
    header, body = recovered.output.split("\n\n", 1)
    assert recovered.status == "success"
    assert json.loads(header)["origin"] == "unverified_summary"
    assert json.loads(body)["model_notes"] == notes


def test_file_rereads_reset_no_progress_per_snapshot_and_keep_same_generation_guard(tmp_path):
    from tools.handlers import run_read

    (tmp_path / "a.py").write_text("evidence that fell out of the compacted prompt")
    session = Session("recovery:file-snapshot", metadata={"workspace_root": str(tmp_path)})
    other = Session("recovery:other-session", metadata={"workspace_root": str(tmp_path)})
    executor = ToolExecutor([ToolLoopGuardHook()])

    def read(owner):
        return executor.execute(
            ToolExecutionRequest("read", "read_file", {"path": "a.py"},
                                 session_id=owner.id, metadata=owner.metadata, session=owner),
            lambda name, args: run_read(**args, _session=owner),
        )

    assert read(session).status == "success"
    assert read(other).status == "success"
    assert read(other).status == "success"
    assert read(other).status == "denied"
    for generation in range(1, 6):
        session.active_snapshot_id = f"snapshot-{generation}"
        result = read(session)
        assert result.status == "success", result.output
        assert "evidence that fell out" in result.output
        assert read(session).status == "denied", "same-generation repetitions remain guarded"
        assert read(other).status == "denied", "another session's guard must not reset"
    session.active_snapshot_id = "snapshot-with-external-change"
    (tmp_path / "a.py").write_text("new bytes from a collaborating agent")
    result = read(session)
    assert result.status == "success"
    assert "new bytes" in result.output and "evidence that fell out" not in result.output
