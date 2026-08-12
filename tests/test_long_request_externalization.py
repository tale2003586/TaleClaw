from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from applications.coding.handoff import (
    CODING_HANDOFF_METADATA_KEY,
    PENDING_CODING_TASK_SUMMARY_METADATA_KEY,
)
from applications.coding.runner import CodingApplication
from applications.coding.session import TaskSessionFactory
from applications.coding.task_state import TASK_STATE_METADATA_KEY
from models.provider import LLMResponse
from applications.turn_coordinator import TurnCoordinator as AgentLoop
from runtime.context import ArtifactStore, ContextBuilder, LongContentDetector
from runtime.context.events import ContextEventType
from runtime.messaging.events import InboundMessage
from runtime.runtime import Runtime
from runtime.sessions.session import Session
from tests.fakes import make_agent_spec
from tools.executor import ToolExecutor


class _InMemorySessions:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}

    def get_or_create(self, session_id: str) -> Session:
        if session_id not in self.sessions:
            self.sessions[session_id] = Session(id=session_id)
        return self.sessions[session_id]

    def save(self, session: Session) -> None:
        self.sessions[session.id] = session


class _Bus:
    def __init__(self) -> None:
        self.outbound = []

    async def publish_outbound(self, message) -> None:
        self.outbound.append(message)


class _CodingRouter:
    agent_spec = make_agent_spec("coding", "coding", "coding")

    def route(self, session, content):
        return SimpleNamespace(
            execution="coding",
            intent="coding_task",
            agent_spec=self.agent_spec,
            confidence=1.0,
            reason="test",
            switched=False,
            switch_message="",
        )


class _Tools:
    def reset_turn_unlocks(self, session) -> None:
        session.metadata["unlocked_tools"] = []

    def schemas_for_turn(self, session, mode):
        return []


class _Provider:
    def __init__(self) -> None:
        self.calls = []

    def chat(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return LLMResponse(
                content="Processed the externalized request.",
                raw_message={
                    "role": "assistant",
                    "content": "Processed the externalized request.",
                },
            )
        return LLMResponse(
            content='{"summary":"Processed the externalized request.","conclusions":[]}'
        )


def _direct_coding_runner(tmp_path: Path) -> tuple[CodingApplication, _InMemorySessions]:
    sessions = _InMemorySessions()
    artifact_store = ArtifactStore(tmp_path / "artifacts")
    coding = CodingApplication(
        sessions=sessions,
        base_runtime=Runtime(
            tools=_Tools(),
            provider=_Provider(),
            model="test-model",
            tool_executor=ToolExecutor([]),
            context_builder=ContextBuilder(),
            max_tokens=1_024,
            max_reasoning_steps=2,
        ),
        workspace_root=tmp_path,
        artifact_store=artifact_store,
        long_content_detector=LongContentDetector(artifact_store),
    )
    coding.factory = TaskSessionFactory(
        sessions,
        root=tmp_path / ".coding_applications",
    )
    return coding, sessions


def _task_session(sessions: _InMemorySessions) -> Session:
    return next(
        session
        for session_id, session in sessions.sessions.items()
        if session_id.startswith("task:")
    )


def _resolve_event_ref(session: Session, event_ref: str):
    assert event_ref.startswith("event://")
    event_id = event_ref.removeprefix("event://")
    return next(event for event in session.event_log if event.event_id == event_id)


def _large_request() -> tuple[str, str, str]:
    instruction = "Inspect the supplied payload and report every TARGET marker."
    marker = "RAW-PAYLOAD-DO-NOT-INLINE-8d5f1c2a|"
    prefix = instruction + "\n\n"
    body_length = 500_000 - len(prefix)
    body = (marker * ((body_length // len(marker)) + 1))[:body_length]
    request = prefix + body
    assert len(request) == 500_000
    return request, instruction, marker


def test_direct_coding_call_records_current_request_in_an_empty_parent(tmp_path) -> None:
    coding, sessions = _direct_coding_runner(tmp_path)
    parent = Session(id="web:direct-empty", active_agent="coding")
    current_request = "Implement the current request event linkage."

    coding.run_coding_task(
        parent_session=parent,
        user_text=current_request,
        agent_spec=_CodingRouter.agent_spec,
    )

    task = _task_session(sessions)
    original_request_ref = task.metadata[TASK_STATE_METADATA_KEY]["objective"][
        "original_request_ref"
    ]
    request_event = _resolve_event_ref(parent, original_request_ref)

    assert original_request_ref
    assert parent.messages[-1]["role"] == "user"
    assert parent.messages[-1]["content"] == current_request
    assert request_event.type == "user_message"
    assert request_event.payload["message"]["content"] == current_request
    assert current_request not in json.dumps(
        task.metadata[CODING_HANDOFF_METADATA_KEY],
        ensure_ascii=False,
    )
    assert task.messages[0]["content"].count(current_request) == 1
    assert task.metadata["original_request_ref"] == original_request_ref
    assert task.messages[0]["metadata"]["original_request_ref"] == original_request_ref
    assert (
        parent.metadata[PENDING_CODING_TASK_SUMMARY_METADATA_KEY][
            "original_request_ref"
        ]
        == original_request_ref
    )
    assert not (coding.factory.root / task.metadata["task_id"] / "memory").exists()

    restarted_parent = Session(
        id=parent.id,
        messages=json.loads(json.dumps(parent.messages)),
        active_agent=parent.active_agent,
        metadata=json.loads(json.dumps(parent.metadata)),
        event_log=[event.to_dict() for event in parent.event_log],
    )
    assert (
        _resolve_event_ref(restarted_parent, original_request_ref).event_id
        == request_event.event_id
    )


def test_direct_coding_call_does_not_reuse_prior_request_or_artifact_refs(tmp_path) -> None:
    coding, sessions = _direct_coding_runner(tmp_path)
    parent = Session(id="web:direct-existing", active_agent="coding")
    old_artifact_ref = {
        "artifact_id": "artifact-old-request",
        "storage_uri": "artifact://artifact-old-request",
        "sha256": "old-request-sha256",
    }
    parent.add_message(
        "user",
        "Implement the previous request.",
        metadata={"artifact_ref": old_artifact_ref},
    )
    old_event_id = parent.event_log[-1].event_id
    current_request = "Implement the new request instead."

    coding.run_coding_task(
        parent_session=parent,
        user_text=current_request,
        agent_spec=_CodingRouter.agent_spec,
    )

    task = _task_session(sessions)
    task_state = task.metadata[TASK_STATE_METADATA_KEY]
    original_request_ref = task_state["objective"]["original_request_ref"]
    request_event = _resolve_event_ref(parent, original_request_ref)

    assert original_request_ref != f"event://{old_event_id}"
    assert request_event.payload["message"]["content"] == current_request
    assert task.metadata["artifact_refs"] == []
    assert task_state["objective"]["source_artifacts"] == []
    assert task_state["artifact_refs"] == []
    assert task.messages[0]["metadata"]["artifact_refs"] == []


def test_direct_coding_call_supersedes_a_tracked_raw_long_request(tmp_path) -> None:
    coding, sessions = _direct_coding_runner(tmp_path)
    parent = Session(id="web:direct-long", active_agent="coding")
    marker = "TRACKED-RAW-REQUEST-CONTENT|"
    current_request = "Inspect this payload.\n\n" + marker * 2_000
    parent.add_message("user", current_request)
    raw_event = parent.event_log[-1]

    coding.run_coding_task(
        parent_session=parent,
        user_text=current_request,
        agent_spec=_CodingRouter.agent_spec,
    )

    task = _task_session(sessions)
    task_state = task.metadata[TASK_STATE_METADATA_KEY]
    original_request_ref = task_state["objective"]["original_request_ref"]
    replacement_event = _resolve_event_ref(parent, original_request_ref)
    replacement_fact = next(
        event
        for event in parent.event_log
        if event.type == ContextEventType.LEGACY_MESSAGE_REPLACED.value
    )

    assert marker in raw_event.payload["message"]["content"]
    assert marker not in replacement_event.payload["message"]["content"]
    assert "artifact://" in replacement_event.payload["message"]["content"]
    assert replacement_fact.payload["replaces_event_id"] == raw_event.event_id
    assert (
        replacement_fact.payload["replacement_event_id"]
        == replacement_event.event_id
    )
    assert raw_event.event_id not in {
        event.event_id for event in parent.active_event_window
    }
    assert replacement_event.event_id in {
        event.event_id for event in parent.active_event_window
    }


def test_500k_inbound_request_is_stored_once_and_only_referenced_downstream(tmp_path) -> None:
    request, instruction, raw_body_marker = _large_request()
    artifact_root = tmp_path / "artifacts"
    artifact_store = ArtifactStore(artifact_root)
    detector = LongContentDetector(artifact_store)
    sessions = _InMemorySessions()
    provider = _Provider()
    coding = CodingApplication(
        sessions=sessions,
        base_runtime=Runtime(
            tools=_Tools(),
            provider=provider,
            model="test-model",
            tool_executor=ToolExecutor([]),
            context_builder=ContextBuilder(),
            max_tokens=1_024,
            max_reasoning_steps=2,
        ),
        workspace_root=tmp_path,
        artifact_store=artifact_store,
        long_content_detector=detector,
    )
    coding.factory = TaskSessionFactory(
        sessions,
        root=tmp_path / ".coding_applications",
    )
    bus = _Bus()
    loop = AgentLoop(
        bus=bus,
        sessions=sessions,
        runtime=None,
        router=_CodingRouter(),
        coding_application=coding,
        long_content_detector=detector,
    )
    inbound = InboundMessage(
        channel="web",
        chat_id="large-request",
        sender="user",
        content=request,
    )

    asyncio.run(loop.run_inbound(inbound))

    metadata_files = list((artifact_root / "metadata").glob("*.json"))
    content_files = list((artifact_root / "content").iterdir())
    assert len(metadata_files) == 1
    assert len(content_files) == 1
    artifact_ref = inbound.metadata["artifact_ref"]
    assert artifact_store.read_artifact(artifact_ref["artifact_id"]) == request

    parent = sessions.sessions["web:large-request"]
    task = next(
        session
        for session_id, session in sessions.sessions.items()
        if session_id.startswith("task:")
    )
    prompt = "\n".join(
        str(message.get("content") or "")
        for call in provider.calls
        for message in call.get("messages", [])
    )
    handoff = task.metadata[CODING_HANDOFF_METADATA_KEY]
    task_state = task.metadata[TASK_STATE_METADATA_KEY]
    session_metadata = json.dumps(
        {"parent": parent.metadata, "task": task.metadata},
        ensure_ascii=False,
        default=str,
    )
    session_messages = json.dumps(
        {"parent": parent.messages, "task": task.messages},
        ensure_ascii=False,
        default=str,
    )
    assert raw_body_marker not in prompt
    assert raw_body_marker not in json.dumps(handoff, ensure_ascii=False)
    assert raw_body_marker not in session_metadata
    assert raw_body_marker not in session_messages
    assert raw_body_marker not in json.dumps(task_state, ensure_ascii=False, default=str)
    assert not (coding.factory.root / task.metadata["task_id"] / "memory").exists()
    assert "current_user_request" not in handoff

    storage_uri = artifact_ref["storage_uri"]
    assert instruction in prompt
    assert storage_uri in prompt
    assert task_state["objective"]["summary"] == instruction
    assert storage_uri in task_state["artifact_refs"]
    assert task_state["objective"]["original_request_ref"].startswith("event://")
    assert bus.outbound
