from datetime import datetime, timedelta, timezone

from memory.domain import MemoryEvidence, MemoryItem, MemoryKind, MemorySourceType, MemoryStatus
from memory.postgres_repository import PostgresMemoryRepository
from runtime.sessions.session import Session
from runtime.sessions.session_store import SessionStore
from tests.postgres_utils import temporary_postgres_schema
from web.memory_view import load_memory_session, read_memory_overview


def test_memory_console_reads_real_persisted_users_tasks_evidence_and_pagination(monkeypatch):
    with temporary_postgres_schema("web_memory_console") as dsn:
        monkeypatch.setenv("MEMORY_DATABASE_URL", dsn)
        monkeypatch.setenv("SESSION_DATABASE_URL", dsn)
        repository = PostgresMemoryRepository(dsn)
        now = datetime.now(timezone.utc)
        for identifier, scope, owner, status in [
            ("personal", "user", "alice", MemoryStatus.ACTIVE),
            ("pending", "user", "alice", MemoryStatus.CANDIDATE),
            ("task-memory", "task", "coding-123", MemoryStatus.ACTIVE),
            ("foreign-user", "user", "bob", MemoryStatus.ACTIVE),
            ("foreign-task", "task", "coding-456", MemoryStatus.ACTIVE),
        ]:
            item = MemoryItem(
                id=identifier, owner_scope=scope, owner_id=owner, kind=MemoryKind.FACT,
                content=identifier, normalized_content=identifier, status=status,
                confidence=.9, salience=.8, valid_from=now - timedelta(days=1),
            )
            evidence = MemoryEvidence(
                id="evidence-" + identifier, memory_id=identifier,
                source_type=MemorySourceType.EXPLICIT_USER, source_ref="message:1",
                excerpt="Evidence for " + identifier,
            )
            repository.create(item, (evidence,))
        sessions = SessionStore(dsn)
        try:
            sessions.save_session(Session(id="web:alice:chat", metadata={
                "user_id": "alice", "active_coding_task_id": "coding-123",
            }))
            sessions.save_session(Session(id="task:coding-123", metadata={
                "kind": "coding_application", "task_id": "coding-123",
                "parent_session_id": "web:alice:chat", "user_id": "alice",
            }))
        finally:
            sessions.close()

        personal = read_memory_overview("alice", limit=1)
        assert personal["available"] is True
        assert personal["counts"] == {"total": 2, "retrievable": 1,
                                      "by_status": {"active": 1, "candidate": 1}}
        assert personal["has_more"] and personal["next_offset"] == 1
        assert personal["items"][0]["evidence"][0]["excerpt"].startswith("Evidence for ")
        second = read_memory_overview("alice", limit=1, offset=1)
        assert second["items"][0]["id"] != personal["items"][0]["id"]
        assert not second["has_more"]

        session = load_memory_session("chat", "alice")
        contextual = read_memory_overview("alice", session=session)
        assert contextual["available"] is True
        assert contextual["counts"]["total"] == 3
        assert contextual["counts"]["retrievable"] == 2
        assert {row["id"] for row in contextual["items"]} == {"personal", "pending", "task-memory"}
        assert {record["id"] for record in contextual["scopes"]} == {"alice", "coding", "coding-123"}
