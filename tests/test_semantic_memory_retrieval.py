from dataclasses import replace
from datetime import datetime, timedelta, timezone

from memory.command_service import MemoryCommandService
from memory.commands import MemoryContext, MemoryWriteProposal
from memory.domain import MemoryKind, MemoryOwnerScope, MemorySourceType, MemoryStatus
from memory.index_sync import MemoryIndexSynchronizer
from memory.semantic_index import IndexedMemoryHit
from memory.semantic_index import QdrantSemanticMemoryIndex
from memory.semantic_retrieval import SemanticMemoryRetrievalService
from tests.fakes.in_memory_memory_index import InMemoryMemoryIndex
from tests.fakes.in_memory_memory_repository import InMemoryMemoryRepository


NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)


def remember(repository, *, owner_id="alice", content="Use concise answers"):
    commands = MemoryCommandService(repository, clock=lambda: NOW)
    context = MemoryContext(user_id=owner_id, session_id=f"web:{owner_id}:a")
    item = commands.remember(MemoryWriteProposal(
        content=content,
        kind=MemoryKind.PREFERENCE,
        owner_scope=MemoryOwnerScope.USER,
        owner_id=owner_id,
        source_type=MemorySourceType.EXPLICIT_USER,
        explicit_user_request=True,
        confidence=0.9,
        salience=0.8,
    ), context)
    return item, context


def test_index_hits_are_backed_by_active_current_postgres_items() -> None:
    repository = InMemoryMemoryRepository()
    item, context = remember(repository)
    index = InMemoryMemoryIndex()
    MemoryIndexSynchronizer(repository, index, clock=lambda: NOW).drain()
    service = SemanticMemoryRetrievalService(repository, index, clock=lambda: NOW)

    result = service.retrieve("concise answers", context)

    assert [hit.item.id for hit in result.hits] == [item.id]
    assert not result.degraded


class StaticIndex:
    def __init__(self, hits):
        self.hits = hits

    def search(self, query, scopes, top_k):
        return self.hits


def test_stale_revoked_and_cross_scope_hits_are_dropped() -> None:
    repository = InMemoryMemoryRepository()
    active, context = remember(repository)
    revoked, _ = remember(repository, content="Use pytest")
    MemoryCommandService(repository, clock=lambda: NOW).revoke(revoked.id, "forgot", context)
    foreign, _ = remember(repository, owner_id="bob", content="Use tables")
    hits = [
        IndexedMemoryHit(active.id, active.version - 1 or 99, 0.9, "user", "alice", "preference"),
        IndexedMemoryHit(revoked.id, repository.get(revoked.id).version, 0.9, "user", "alice", "preference"),
        IndexedMemoryHit(foreign.id, foreign.version, 0.9, "user", "bob", "preference"),
    ]

    result = SemanticMemoryRetrievalService(
        repository,
        StaticIndex(hits),
        clock=lambda: NOW,
    ).retrieve("use", context)

    assert result.hits == ()
    assert result.drop_reasons == {
        "stale_version": 1,
        "inactive_or_expired": 1,
        "scope_mismatch": 1,
    }


def test_index_failure_falls_back_to_scope_limited_active_items() -> None:
    repository = InMemoryMemoryRepository()
    item, context = remember(repository)
    remember(repository, owner_id="bob", content="Concise answers for Bob")
    index = InMemoryMemoryIndex()
    index.fail_search = True

    result = SemanticMemoryRetrievalService(
        repository,
        index,
        clock=lambda: NOW,
    ).retrieve("concise", context)

    assert result.degraded
    assert [hit.item.id for hit in result.hits] == [item.id]


def test_expired_item_is_not_returned_in_fallback() -> None:
    repository = InMemoryMemoryRepository()
    item, context = remember(repository)
    repository.items[item.id] = replace(
        item,
        valid_until=NOW + timedelta(hours=1),
    )
    index = InMemoryMemoryIndex()
    index.fail_search = True

    result = SemanticMemoryRetrievalService(
        repository,
        index,
        clock=lambda: NOW + timedelta(hours=2),
    ).retrieve("concise", context)

    assert result.hits == ()


def test_qdrant_semantic_payload_excludes_content_and_evidence() -> None:
    repository = InMemoryMemoryRepository()
    item, _ = remember(repository)

    class Embeddings:
        vector_size = 2

        def embed(self, text):
            return [1.0, 0.0]

    class Client:
        points = []

        def collection_exists(self, collection):
            return True

        def upsert(self, *, collection_name, points):
            self.points.extend(points)

    client = Client()
    index = QdrantSemanticMemoryIndex(
        url="http://unused",
        collection="semantic-test",
        embeddings=Embeddings(),
        client=client,
    )

    index.upsert(item)

    payload = client.points[0].payload
    assert set(payload) == {
        "memory_id", "memory_version", "owner_scope", "owner_id", "kind",
        "status", "valid_until", "content_digest", "indexed_at",
    }
    assert "content" not in payload
    assert "evidence" not in payload
