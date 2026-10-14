from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from memory.commands import MemoryTransition
from memory.domain import MemoryEvidence, MemoryItem, MemoryStatus
from memory.postgres_repository import PostgresMemoryRepository
from memory.repository import InvalidTransition, ScopeDenied, VersionConflict
from tests.postgres_utils import temporary_postgres_schema
from web.memory_management import MemoryStorageUnavailable, apply_memory_action
from web.memory_view import read_memory_overview


def create(repository, identifier="candidate", **changes):
    now = datetime.now(timezone.utc)
    item = MemoryItem(**{
        "id": identifier, "owner_scope": "user", "owner_id": "alice", "kind": "fact",
        "content": f"Literal %_\\ {identifier}", "normalized_content": f"literal {identifier}",
        "status": "candidate", "confidence": .8, "salience": .8,
        "valid_from": now - timedelta(days=2), **changes,
    })
    evidence = MemoryEvidence(id="evidence-" + identifier, memory_id=identifier,
                              source_type="explicit_user", source_ref="message:1", excerpt="original evidence")
    repository.create(item, (evidence,))
    return item


def test_real_console_action_cas_audit_evidence_outbox_and_unauthorized_scope(monkeypatch):
    with temporary_postgres_schema("web_memory_actions") as dsn:
        monkeypatch.setenv("MEMORY_DATABASE_URL", dsn)
        repository = PostgresMemoryRepository(dsn)
        create(repository)
        create(repository, "foreign", owner_id="bob")
        create(repository, "shared", owner_scope="project", owner_id="repo")
        result = apply_memory_action("alice", {"id": "candidate", "expected_version": 1, "action": "confirm", "reason": "reviewed"})
        assert result["item"]["status"] == "active" and result["item"]["version"] == 2
        assert result["item"]["evidence"][0]["excerpt"] == "original evidence"
        assert result["item"]["source_types"] == ["explicit_user"]
        persisted = repository.get("candidate")
        assert persisted.metadata["console_action"]["actor"] == "alice"
        assert persisted.metadata["console_action"]["reason"] == "reviewed"
        assert [(event.memory_version, event.operation.value) for event in repository.list_index_events()] == [(2, "upsert")]
        with pytest.raises(VersionConflict):
            apply_memory_action("alice", {"id": "candidate", "expected_version": 1, "action": "revoke"})
        for identifier in ("foreign", "shared"):
            with pytest.raises(ScopeDenied):
                apply_memory_action("alice", {"id": identifier, "expected_version": 999, "action": "confirm"})
            assert repository.get(identifier).version == 1
        result = apply_memory_action("alice", {"id": "candidate", "expected_version": 2, "action": "revoke"})
        assert result["item"]["status"] == "revoked" and not result["item"]["retrievable"]
        assert [event.operation.value for event in repository.list_index_events()] == ["upsert", "delete"]
        assert len(repository.list_evidence("candidate")) == 1


def test_real_search_is_literal_scoped_and_paginates_filtered_set(monkeypatch):
    with temporary_postgres_schema("web_memory_search") as dsn:
        monkeypatch.setenv("MEMORY_DATABASE_URL", dsn)
        repository = PostgresMemoryRepository(dsn)
        create(repository, "match-1", kind="decision")
        create(repository, "match-2", kind="decision")
        create(repository, "wildcard", content="Literal XAAAA wildcard")
        create(repository, "foreign", owner_id="bob", kind="decision")
        result = read_memory_overview("alice", query="%_\\", status="candidate", kind="decision", limit=1)
        assert result["available"] and result["filtered_total"] == 2 and result["counts"]["total"] == 3
        assert len(result["items"]) == 1 and result["has_more"] and result["next_offset"] == 1
        second = read_memory_overview("alice", query="%_\\", status="candidate", kind="decision", limit=1, offset=1)
        assert second["items"][0]["id"] != result["items"][0]["id"] and not second["has_more"]
        assert read_memory_overview("alice", query="alice")["filtered_total"] == 3
        assert read_memory_overview("alice", query="WILDCARD")["filtered_total"] == 1


def test_real_concurrent_confirm_is_once_only_and_outbox_failure_rolls_back(monkeypatch):
    with temporary_postgres_schema("web_memory_cas") as dsn:
        monkeypatch.setenv("MEMORY_DATABASE_URL", dsn)
        repository = PostgresMemoryRepository(dsn)
        create(repository)
        payload = {"id": "candidate", "expected_version": 1, "action": "confirm"}
        def confirm():
            try:
                return apply_memory_action("alice", payload)["item"]["status"]
            except VersionConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(lambda _: confirm(), range(2))) == ["active", "conflict"]
        assert repository.get("candidate").version == 2 and len(repository.list_index_events()) == 1
        create(repository, "rollback")
        def fail_outbox(*args):
            raise RuntimeError("DSN:SECRET")
        monkeypatch.setattr(PostgresMemoryRepository, "_schedule", fail_outbox)
        with pytest.raises(MemoryStorageUnavailable) as error:
            apply_memory_action("alice", {**payload, "id": "rollback"})
        assert "SECRET" not in str(error.value)
        assert repository.get("rollback").version == 1 and repository.get("rollback").status is MemoryStatus.CANDIDATE
        assert len(repository.list_index_events()) == 1 and len(repository.list_evidence("rollback")) == 1


def test_real_expired_confirm_and_terminal_transition_are_rejected_under_lock(monkeypatch):
    with temporary_postgres_schema("web_memory_expired") as dsn:
        monkeypatch.setenv("MEMORY_DATABASE_URL", dsn)
        repository = PostgresMemoryRepository(dsn)
        create(repository, "past", valid_until=datetime.now(timezone.utc) - timedelta(days=1))
        create(repository, "rejected", status="rejected")
        for identifier in ("past", "rejected"):
            with pytest.raises(InvalidTransition):
                apply_memory_action("alice", {"id": identifier, "expected_version": 1, "action": "confirm"})
            assert repository.get(identifier).version == 1
        assert repository.list_index_events() == []
