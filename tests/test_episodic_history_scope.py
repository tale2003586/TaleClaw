from types import SimpleNamespace

import pytest

from memory.episodic_retrieval import (
    EpisodicBoundary,
    EpisodicHistoryRetrievalService,
)
from memory.qdrant_index import QdrantMemoryVectorIndex
from memory.vector_index import MemoryHit, MemoryRecord
from memory.vector_runtime import history_vector_scope_for_session
from runtime.context.retrieval import ContextRetrievalService
from runtime.sessions.session import Session


class FilteredRecordingIndex:
    def __init__(self) -> None:
        self.records: list[MemoryRecord] = []
        self.searches: list[dict] = []

    def upsert(self, record: MemoryRecord) -> None:
        self.records.append(record)

    def search_filtered(self, **kwargs) -> list[MemoryHit]:
        self.searches.append(dict(kwargs))
        filters = kwargs["filters"]
        hits = []
        for record in self.records:
            payload = {
                "source_type": record.source_type,
                **{f"metadata.{key}": value for key, value in record.metadata.items()},
            }
            if not all(str(payload.get(key, "")) == str(value) for key, value in filters.items()):
                continue
            hits.append(MemoryHit(
                id=record.id,
                text=record.text,
                score=0.9,
                scope=record.scope,
                source_type=record.source_type,
                source_ref=record.source_ref,
                metadata=record.metadata,
            ))
        return hits


def session(user: str, chat: str) -> Session:
    return Session(id=f"web:{user}:{chat}", metadata={"user_id": user})


def record_for(value: Session, text: str) -> MemoryRecord:
    return MemoryRecord(
        id=f"{value.id}:turn",
        text=text,
        scope=history_vector_scope_for_session(value),
        source_type="session_turn",
        source_ref=f"{value.id}:1",
        metadata={"session_id": value.id, "user_id": value.metadata["user_id"]},
    )


def test_boundary_requires_current_session_or_trusted_coding_scope() -> None:
    with pytest.raises(ValueError):
        EpisodicBoundary(user_id="alice")
    boundary = EpisodicBoundary(user_id="alice", session_id="web:alice:a")
    assert boundary.filters() == {
        "source_type": "session_turn",
        "metadata.user_id": "alice",
        "metadata.session_id": "web:alice:a",
    }


def test_normal_session_scope_is_session_specific() -> None:
    session_a = session("alice", "a")
    session_b = session("alice", "b")
    assert history_vector_scope_for_session(session_a) == "session:web:alice:a"
    assert history_vector_scope_for_session(session_b) == "session:web:alice:b"
    assert history_vector_scope_for_session(session_a) != history_vector_scope_for_session(session_b)


def test_new_session_cannot_retrieve_other_session_raw_turn() -> None:
    session_a = session("alice", "a")
    session_b = session("alice", "b")
    index = FilteredRecordingIndex()
    index.upsert(record_for(session_a, "unique phrase from session A"))
    service = ContextRetrievalService(history_vector_index=index)

    block_b, hits_b = service.retrieve_history(
        session=session_b,
        current_request="unique phrase",
        active_turn_messages=[],
    )
    block_a, hits_a = service.retrieve_history(
        session=session_a,
        current_request="unique phrase",
        active_turn_messages=[],
    )

    assert hits_b == []
    assert block_b == ""
    assert len(hits_a) == 1
    assert "<episodic_history" in block_a
    assert "past_event" in block_a
    assert index.searches[0]["filters"]["metadata.session_id"] == session_b.id
    assert index.searches[1]["filters"]["metadata.session_id"] == session_a.id


def test_coding_boundary_uses_task_and_trusted_workspace() -> None:
    coding = Session(
        id="task:coding-a",
        active_agent="coding",
        metadata={
            "kind": "coding_application",
            "user_id": "alice",
            "task_id": "coding-a",
            "workspace_root": "/workspace/a",
            "repository": "/workspace/a/repo",
        },
    )

    filters = EpisodicBoundary.from_session(coding).filters()

    assert filters["metadata.task_id"] == "coding-a"
    assert filters["metadata.workspace_id"] == "/workspace/a"
    assert filters["metadata.project_id"] == "/workspace/a/repo"


def test_service_failure_returns_empty_without_user_scope_fallback() -> None:
    class FailingIndex:
        def search_filtered(self, **kwargs):
            raise RuntimeError("unavailable")

    result = EpisodicHistoryRetrievalService(FailingIndex()).retrieve(
        "query",
        EpisodicBoundary(user_id="alice", session_id="web:alice:a"),
    )

    assert result.hits == ()
    assert result.degraded


def test_qdrant_filtered_search_builds_all_must_conditions() -> None:
    index = object.__new__(QdrantMemoryVectorIndex)
    index.collection = "history"
    index.embeddings = SimpleNamespace(embed=lambda text: [1.0, 0.0])
    captured = {}

    def query_points(*, vector, query_filter, limit):
        captured["filter"] = query_filter
        return []

    index._query_points = query_points
    filters = EpisodicBoundary(
        user_id="alice",
        session_id="web:alice:a",
    ).filters()

    assert index.search_filtered(
        query="query",
        filters=filters,
        top_k=3,
    ) == []
    conditions = {
        condition.key: condition.match.value
        for condition in captured["filter"].must
    }
    assert conditions == filters
