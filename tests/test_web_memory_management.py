from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from memory.domain import MemoryItem, MemoryStatus
from memory.repository import InvalidTransition, ScopeDenied, VersionConflict
from web import memory_management, memory_view


def candidate(**changes):
    now = datetime.now(timezone.utc)
    values = dict(id="candidate", owner_scope="user", owner_id="alice", kind="fact",
                  content="safe content", normalized_content="safe content", status="candidate",
                  confidence=.8, salience=.8, valid_from=now - timedelta(days=1))
    return MemoryItem(**{**values, **changes})


class FakeRepository:
    def __init__(self, item):
        self.item = item
        self.calls = []

    def transition_scoped(self, command, scopes, *, expected_status):
        self.calls.append((command, scopes, expected_status))
        if self.item.owner not in scopes:
            raise ScopeDenied(command.memory_id)
        if command.expected_version != self.item.version:
            raise VersionConflict(command.memory_id)
        if self.item.status != expected_status:
            raise InvalidTransition("changed")
        self.item = self.item.transitioned(command.target_status, metadata=command.metadata)
        return self.item, []


def install(monkeypatch, item=None):
    repository = FakeRepository(item or candidate())
    monkeypatch.setattr(memory_management, "PostgresMemoryRepository",
                        lambda **kwargs: repository if kwargs == {"initialize_schema": False} else pytest.fail("schema initialization"))
    return repository


def test_confirm_carries_client_version_and_host_audit_without_exposing_metadata(monkeypatch):
    repository = install(monkeypatch)
    result = memory_management.apply_memory_action("alice", {
        "id": "candidate", "expected_version": 1, "action": "confirm", "reason": "verified personally",
    })
    command, scopes, expected_status = repository.calls[0]
    assert command.expected_version == 1 and expected_status is MemoryStatus.CANDIDATE
    assert [(owner.scope.value, owner.id) for owner in scopes] == [("user", "alice")]
    assert command.metadata["console_action"]["actor"] == "alice"
    assert command.metadata["console_action"]["action"] == "confirm"
    assert command.metadata["console_action"]["reason"] == "verified personally"
    assert result["item"]["status"] == "active" and result["item"]["version"] == 2
    assert result["index_sync"] == "queued" and "metadata" not in result["item"]


@pytest.mark.parametrize("changes", [{"expected_version": True}, {"expected_version": "1"},
    {"expected_version": 0}, {"action": "delete"}, {"id": ""}, {"session_id": []},
    {"reason": "x" * 1001}, {"reason": "bad\x00input"}, {"owner_id": "bob"}, {"scopes": []}])
def test_invalid_payload_is_rejected_before_loading_storage(monkeypatch, changes):
    calls = []
    monkeypatch.setattr(memory_management, "PostgresMemoryRepository", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ValueError):
        memory_management.apply_memory_action("alice", {
            "id": "candidate", "expected_version": 1, "action": "confirm", **changes,
        })
    assert calls == []


@pytest.mark.parametrize("scope,owner", [("user", "bob"), ("application", "coding"),
                                      ("workspace", "shared"), ("project", "repo"), ("task", "unverified")])
def test_personal_actions_never_change_other_user_or_shared_scope(monkeypatch, scope, owner):
    repository = install(monkeypatch, candidate(owner_scope=scope, owner_id=owner))
    with pytest.raises(ScopeDenied):
        memory_management.apply_memory_action("alice", {"id": "candidate", "expected_version": 1, "action": "confirm"})
    assert repository.item.version == 1


def test_only_a_server_verified_bound_task_is_manageable(monkeypatch):
    repository = install(monkeypatch, candidate(owner_scope="task", owner_id="task-1"))
    session = {"id": "web:alice:chat", "metadata": {"active_coding_task_id": "task-1", "task_id": "forged"}}
    monkeypatch.setattr(memory_management, "load_memory_session", lambda session_id, user_id: session)
    monkeypatch.setattr(memory_view, "_load_stored_session", lambda storage_id: {
        "id": "task:task-1", "metadata": {"kind": "coding_application", "task_id": "task-1",
        "parent_session_id": "web:alice:chat", "user_id": "alice", "project_id": "shared"},
    })
    result = memory_management.apply_memory_action("alice", {
        "id": "candidate", "expected_version": 1, "action": "confirm", "session_id": "chat",
    })
    assert result["item"]["status"] == "active"
    assert [(owner.scope.value, owner.id) for owner in repository.calls[0][1]] == [("user", "alice"), ("task", "task-1")]


def test_client_cas_and_lifecycle_conflicts_remain_distinct_from_storage_errors(monkeypatch):
    repository = install(monkeypatch)
    with pytest.raises(VersionConflict):
        memory_management.apply_memory_action("alice", {"id": "candidate", "expected_version": 2, "action": "confirm"})
    repository.item = replace(repository.item, status=MemoryStatus.ACTIVE)
    with pytest.raises(InvalidTransition):
        memory_management.apply_memory_action("alice", {"id": "candidate", "expected_version": 1, "action": "reject"})
    monkeypatch.setattr(memory_management, "PostgresMemoryRepository", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("DSN:SECRET")))
    with pytest.raises(memory_management.MemoryStorageUnavailable) as error:
        memory_management.apply_memory_action("alice", {"id": "candidate", "expected_version": 1, "action": "confirm"})
    assert "SECRET" not in str(error.value)
