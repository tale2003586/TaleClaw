from datetime import datetime, timezone
from types import SimpleNamespace

from memory.command_service import MemoryCommandService
from memory.commands import MemoryContext, MemoryWriteProposal
from memory.domain import MemoryKind, MemoryOwnerScope, MemorySourceType
from memory.index_sync import MemoryIndexSynchronizer
from memory.semantic_retrieval import SemanticMemoryRetrievalService
from runtime.trace.memory_injection import content_digest
from runtime.trace.summary import build_trace_summary_payload
from runtime.context import ContextBuilder
from tests.fakes.in_memory_memory_index import InMemoryMemoryIndex
from tests.fakes.in_memory_memory_repository import InMemoryMemoryRepository


NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


def _service(enabled: bool):
    repository = InMemoryMemoryRepository()
    index = InMemoryMemoryIndex()
    context = MemoryContext(user_id="alice", session_id="web:alice:one")
    item = MemoryCommandService(repository, clock=lambda: NOW).remember(
        MemoryWriteProposal(
            content="A secret-shaped but allowed preference that must not appear in trace",
            kind=MemoryKind.PREFERENCE,
            owner_scope=MemoryOwnerScope.USER,
            owner_id="alice",
            source_type=MemorySourceType.EXPLICIT_USER,
            explicit_user_request=True,
            confidence=.9,
            salience=.8,
        ),
        context,
    )
    MemoryIndexSynchronizer(repository, index, clock=lambda: NOW).drain()
    return SemanticMemoryRetrievalService(
        repository,
        index,
        clock=lambda: NOW,
        injection_trace_enabled=enabled,
    ), context, item


def test_injection_trace_records_decision_without_full_content() -> None:
    service, context, item = _service(True)
    service.retrieve("allowed preference", context)
    events = service.drain_trace_events()
    explanation = next(
        event for event in events if event["event"] == "memory.injection.explained"
    )
    payload = explanation["payload"]
    candidate = payload["candidates"][0]

    assert candidate["memory_id"] == item.id
    assert candidate["selected"] is True
    assert candidate["content_digest"] == content_digest(item.content)
    assert item.content not in str(payload)
    assert payload["selected_count"] == 1
    assert payload["pressure_level"] == "unknown"


def test_trace_flag_off_preserves_existing_single_retrieval_event() -> None:
    service, context, _ = _service(False)
    result = service.retrieve("allowed preference", context)
    events = service.drain_trace_events()
    assert result.hits
    assert [event["event"] for event in events] == ["memory.semantic.retrieved"]


def test_trace_callback_failure_does_not_block_retrieval() -> None:
    service, context, _ = _service(True)
    service.trace = lambda *args: (_ for _ in ()).throw(RuntimeError("trace offline"))
    result = service.retrieve("allowed preference", context)
    assert result.hits


def test_trace_summary_aggregates_injection_explanations() -> None:
    event = {
        "event": "memory.injection.explained",
        "payload": {
            "selected_count": 2,
            "filtered_count": 1,
            "pressure_level": "high",
        },
    }
    summary = build_trace_summary_payload(
        run_state={}, metrics={}, report={}, events=[event]
    )["memory"]
    assert summary["injection_traces"] == 1
    assert summary["injected_count"] == 2
    assert summary["filtered_count"] == 1
    assert summary["last_pressure_level"] == "high"


def test_context_observation_links_pressure_to_queued_injection_trace() -> None:
    event = {
        "event": "memory.injection.explained",
        "payload": {"pressure_level": "unknown"},
    }
    session = SimpleNamespace(metadata={"memory_trace_events": [event]})
    report = SimpleNamespace(metadata={"context_pressure": {"level": "critical"}})

    ContextBuilder(injection_trace_enabled=True)._annotate_memory_injection_trace(
        session,
        report,
    )

    assert event["payload"]["pressure_level"] == "critical"
