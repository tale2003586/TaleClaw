from __future__ import annotations

from types import SimpleNamespace

from runtime.context import ArtifactStore
from runtime.context.artifact_access import load_artifact_access_states
from runtime.context.builder import ContextBuilder
from runtime.execution.reasoning_loop import _is_loop_guard_denial
from runtime.sessions import Session
from tools.executor import ToolExecutionRequest, ToolExecutor
from tools.hooks import ArtifactAccessGuardHook
from tools.tool_registry import build_lead_tool_registry


def _call(executor, registry, session, arguments, call_id="call"):
    return executor.execute(
        ToolExecutionRequest(
            call_id,
            "read_artifact",
            arguments,
            session_id=session.id,
            metadata=session.metadata,
        ),
        lambda name, args: registry.execute(
            name,
            args,
            session=session,
            mode="bot",
        ),
    )


def test_range_coverage_eof_and_duplicate_guard_are_runtime_managed(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_artifact("0123456789" * 50, artifact_type="user_input")
    registry = build_lead_tool_registry(artifact_store=store)
    executor = ToolExecutor([ArtifactAccessGuardHook()])
    session = Session("artifact:ranges")

    first = _call(executor, registry, session, {
        "artifact_ref": ref.storage_uri,
        "limit": 200,
    }, "first")
    state = load_artifact_access_states(session.metadata)[ref.storage_uri]
    assert first.metadata["artifact_access"]["new_coverage_chars"] == 200
    assert state.covered_ranges == [(0, 200)]
    assert state.access_status == "partially_accessed"

    duplicate = _call(executor, registry, session, {
        "artifact_ref": ref.storage_uri,
        "limit": 200,
    }, "duplicate")
    assert duplicate.status == "denied"
    assert "already been covered" in duplicate.output

    tail = _call(executor, registry, session, {
        "artifact_ref": ref.storage_uri,
        "offset": 200,
        "limit": 300,
    }, "tail")
    state = load_artifact_access_states(session.metadata)[ref.storage_uri]
    assert tail.status == "success"
    assert state.covered_ranges == [(0, 500)]
    assert state.eof is True
    assert state.access_status == "fully_accessed"

    after_eof = _call(executor, registry, session, {
        "artifact_ref": ref.storage_uri,
        "offset": 0,
        "limit": 500,
    }, "after-eof")
    assert after_eof.status == "denied"


def test_different_range_and_normalized_search_query_behavior(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_artifact("TARGET one\n" + "x" * 500 + "\nTARGET two")
    registry = build_lead_tool_registry(artifact_store=store)
    executor = ToolExecutor([ArtifactAccessGuardHook()])
    session = Session("artifact:search")

    assert _call(executor, registry, session, {
        "artifact_ref": ref.storage_uri,
        "offset": 0,
        "limit": 200,
    }, "range-1").status == "success"
    assert _call(executor, registry, session, {
        "artifact_ref": ref.storage_uri,
        "offset": 200,
        "limit": 200,
    }, "range-2").status == "success"
    assert _call(executor, registry, session, {
        "artifact_ref": ref.storage_uri,
        "query": " TARGET   one ",
    }, "search-1").status == "success"
    repeated = _call(executor, registry, session, {
        "artifact_ref": ref.storage_uri,
        "query": "target one",
    }, "search-2")
    assert repeated.status == "denied"
    state = load_artifact_access_states(session.metadata)[ref.storage_uri]
    assert "target one" in state.normalized_queries
    assert state.repeated_read_calls >= 1


def test_context_reconstructs_artifact_access_summary_after_history_compaction(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_artifact("z" * 250)
    registry = build_lead_tool_registry(artifact_store=store)
    executor = ToolExecutor([ArtifactAccessGuardHook()])
    session = Session("artifact:context")
    session.add_message("user", "只记录附件状态", metadata={
        "attachments": [{
            "name": "report.pdf",
            "media_type": "application/pdf",
            "size_bytes": 1000,
            "content_state": "externalized",
            "artifact_ref": ref.to_dict(),
        }],
    })
    _call(executor, registry, session, {
        "artifact_ref": ref.storage_uri,
        "limit": 200,
    })
    session.archive_boundary_seq = max(0, len(session.event_log))

    context = ContextBuilder().build(
        session=session,
        profile=SimpleNamespace(system_prompt="base", tool_mode="bot"),
    )

    rendered = "\n".join(str(item.get("content") or "") for item in context.messages)
    assert '<task-state source="runtime-generated" instructions="false"' in rendered
    assert '<artifact-access-state source="runtime-generated" instructions="false">' in rendered
    assert '"covered_ranges":[[0,200]]' in rendered


def test_second_no_progress_artifact_attempt_triggers_reasoning_loop_stop(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_artifact("x" * 200)
    registry = build_lead_tool_registry(artifact_store=store)
    executor = ToolExecutor([ArtifactAccessGuardHook()])
    session = Session("artifact:loop-stop")
    arguments = {"artifact_ref": ref.storage_uri, "limit": 200}

    assert _call(executor, registry, session, arguments, "read").status == "success"
    first_duplicate = _call(executor, registry, session, arguments, "duplicate-1")
    second_duplicate = _call(executor, registry, session, arguments, "duplicate-2")

    assert _is_loop_guard_denial(first_duplicate) is False
    assert _is_loop_guard_denial(second_duplicate) is True
    assert "no_progress_count=2" in second_duplicate.output
