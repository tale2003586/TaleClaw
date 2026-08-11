from __future__ import annotations

from types import SimpleNamespace

import pytest

from applications.coding.task_state import Objective, TaskPhase, TaskState, load_task_state
from applications.coding.task_state import ensure_task_state
from runtime.context.builder import ContextBuilder
from runtime.context import ArtifactStore, LongContentDetector
from runtime.execution.failure_reasons import StopDecision, StopReason
from runtime.execution.state import RunExecutionState
from applications.turn_coordinator import TurnCoordinator as AgentLoop
from runtime.messaging.events import InboundMessage
from runtime.sessions import Session
from runtime.task_state import (
    TaskStateRunObserver,
    TaskStateCorePatch,
    TaskStateValidationError,
    apply_task_state_core_patch,
    ensure_task_state_core,
    load_task_state_core,
)
from runtime.task_state.models import TaskStatus
from tools.tool_registry import build_lead_tool_registry


def _profile(mode: str):
    return SimpleNamespace(instructions="base", tool_mode=mode)


def test_chat_context_does_not_create_task_state_core() -> None:
    session = Session("chat:core")
    session.add_message("user", "回答 123")
    builder = ContextBuilder()

    first = builder.build(session=session, agent_spec=_profile("bot"))
    state = load_task_state_core(session)
    second = builder.build(session=session, agent_spec=_profile("bot"))

    assert state is None
    assert "task_state" not in session.metadata
    assert all("<task-state " not in item["content"] for item in first.messages)
    assert all("<task-state " not in item["content"] for item in second.messages)


def test_hybrid_uses_core_schema_without_coding_phase() -> None:
    registry = build_lead_tool_registry()
    session = Session("hybrid:core", active_agent="hybrid")
    session.add_message("user", "回答问题")
    ContextBuilder().build(session=session, agent_spec=_profile("hybrid"))
    registry.execute(
        "tool_search",
        {"query": "select:update_task_state"},
        session=session,
        mode="hybrid",
    )

    schema = next(
        item for item in registry.schemas_for_turn(session, "hybrid")
        if item["function"]["name"] == "update_task_state"
    )
    properties = schema["function"]["parameters"]["properties"]
    assert "current_focus" in properties
    assert "phase" not in properties

    assert load_task_state_core(session) is None


def test_coding_task_state_is_shared_core_with_coding_extension_and_old_payload() -> None:
    legacy = {
        "objective": {"summary": "修复测试"},
        "phase": "implementation",
        "plan": [{"id": "plan:1", "description": "修改", "status": "in_progress"}],
        "version": 4,
    }
    session = Session("coding:legacy", metadata={"task_state": legacy})

    state = load_task_state(session)

    assert state.objective.summary == "修复测试"
    assert state.phase == TaskPhase.IMPLEMENTATION
    assert state.version == 4
    assert state.to_dict()["schema_version"] == 2
    assert state.to_dict()["extensions"]["coding"]["phase"] == "implementation"


def test_legacy_flat_coding_state_is_persisted_as_v2_on_ensure() -> None:
    session = Session("coding:legacy-ensure", metadata={"task_state": {
        "objective": {"summary": "继续旧任务"},
        "phase": "exploration",
        "version": 2,
    }})

    state = ensure_task_state(session, objective_summary="ignored")

    assert state.phase == TaskPhase.EXPLORATION
    assert session.metadata["task_state"]["schema"] == "task_state"
    assert session.metadata["task_state"]["schema_version"] == 2
    assert session.metadata["task_state"]["extensions"]["coding"]["phase"] == "exploration"


def test_core_lifecycle_has_no_task_phase_and_rejects_conflicts_or_terminal_revival() -> None:
    state = load_task_state_core(Session(
        "core:loaded",
        metadata={"task_state": {
            "schema": "task_state",
            "schema_version": 2,
            "core": {"task_id": "x", "version": 3, "objective": "回答", "status": "active"},
            "extensions": {},
        }},
    ))
    assert state is not None

    with pytest.raises(TaskStateValidationError, match="version conflict"):
        apply_task_state_core_patch(state, TaskStateCorePatch(
            base_version=2,
            current_focus="错误版本",
        ))

    completed = apply_task_state_core_patch(state, TaskStateCorePatch(
        base_version=3,
        pending_replace=[],
        blockers_replace=[],
        completion_basis_add=["问题已回答"],
        requested_status=TaskStatus.COMPLETED,
    ))
    with pytest.raises(TaskStateValidationError, match="illegal|terminal"):
        apply_task_state_core_patch(completed, TaskStateCorePatch(
            base_version=completed.version,
            requested_status=TaskStatus.ACTIVE,
        ))


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (StopReason.COMPLETED, TaskStatus.COMPLETED),
        (StopReason.USER_CANCELLED, TaskStatus.CANCELLED),
        (StopReason.HARD_BUDGET_EXCEEDED, TaskStatus.FAILED),
        (StopReason.TOOL_UNAVAILABLE, TaskStatus.BLOCKED),
    ],
)
def test_task_state_observer_owns_run_status_projection(reason, expected) -> None:
    session = Session(f"task-observer:{reason}")
    ensure_task_state_core(session, objective="finish the task")
    execution = RunExecutionState(
        stop_decision=StopDecision(reason=reason, message="finished or stopped"),
    )

    TaskStateRunObserver().after_run(session=session, execution=execution)

    state = load_task_state_core(session)
    assert state is not None
    assert state.status is expected
    assert execution.stop_decision.task_state_version == state.version


def test_coding_context_snapshot_does_not_replace_task_state_authority() -> None:
    session = Session(
        "coding:snapshot",
        metadata={
            "task_state": TaskState(
                objective=Objective("权威目标"),
                phase=TaskPhase.PLANNING,
            ).to_dict(),
            "coding_context_state": {
                "version": 2,
                "task_state_version": 999,
                "generation": 9,
                "prompt_tail_start_index": 0,
                "compacted_until_index": 0,
            },
        },
    )

    assert load_task_state(session).objective.summary == "权威目标"
    assert load_task_state_core(session).version == 1


def test_attachment_metadata_is_separate_and_instruction_free(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    detector = LongContentDetector(
        store,
        max_tokens=100,
        max_chars=100,
        max_bytes=100,
        token_estimator=lambda _text: 1,
    )
    loop = AgentLoop(
        bus=None,
        sessions=None,
        runtime=None,
        router=None,
        long_content_detector=detector,
    )
    inbound = InboundMessage(
        channel="web",
        chat_id="attachment",
        sender="user",
        content="123",
        metadata={"attachments": [{
            "name": "2502.12110v11.pdf",
            "media_type": "application/pdf",
            "size_bytes": 103556,
            "content": "parsed PDF text",
        }]},
    )

    refs = loop._externalize_inbound_artifacts(inbound)
    assert inbound.content == "123"
    assert len(refs) == 1
    assert "content" not in inbound.metadata["attachments"][0]

    session = Session("chat:attachment")
    session.add_message("user", inbound.content, metadata=inbound.metadata)
    context = ContextBuilder().build(session=session, agent_spec=_profile("bot"))
    rendered = "\n".join(str(item.get("content") or "") for item in context.messages)
    attachment_block = next(
        item["content"] for item in context.messages
        if item.get("metadata", {}).get("kind") == "user_attachments"
    )

    assert context.report.to_dict()["sections"]["current_request"]["raw_chars"] == 3
    assert 'name="2502.12110v11.pdf"' in attachment_block
    assert 'media_type="application/pdf"' in attachment_block
    assert 'size_bytes="103556"' in attachment_block
    assert refs[0]["storage_uri"] in attachment_block
    assert "Use read_artifact" not in attachment_block
    assert "next_offset" not in attachment_block
    assert "Attachment presence alone" in context.messages[0]["content"]
    assert "read_artifact" not in attachment_block.casefold()
