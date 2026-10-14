from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from web import memory_view


NOW = datetime.now(timezone.utc)


def item(memory_id="preference-1", **overrides):
    return {
        "id": memory_id, "owner_scope": "user", "owner_id": "alice",
        "kind": "preference", "content": "Prefer concise replies.", "status": "active",
        "confidence": 1.0, "salience": 0.8, "version": 1,
        "valid_from": NOW - timedelta(days=1), "valid_until": None,
        "last_confirmed_at": NOW, "created_at": NOW, "updated_at": NOW,
        "supersedes_id": None, "metadata": {"internal": "not exposed"}, **overrides,
    }


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class ReadOnlyCatalog:
    def __init__(self, items=(), evidence=(), sessions=()):
        self.items, self.evidence, self.sessions = list(items), list(evidence), list(sessions)
        self.calls = []
        self.closed = False

    def execute(self, query, params=()):
        self.calls.append((query, params))
        if query.startswith("SET TRANSACTION READ ONLY"):
            return Result([])
        if "FROM sessions" in query:
            return Result([row for row in self.sessions if row["id"] == params[0]])
        if "FROM memory_evidence" in query:
            return Result([row for row in self.evidence if row["memory_id"] in params[0]])
        assert "owner_scope = %s AND owner_id = %s" in query
        total = "GROUP BY status" in query
        scope_params = params[2:] if total else params[:-2]
        owners = set(zip(scope_params[::2], scope_params[1::2]))
        rows = [row for row in self.items if (row["owner_scope"], row["owner_id"]) in owners]
        if total:
            counts = {}
            for row in rows:
                status = row["status"]
                tally = counts.setdefault(status, {"status": status, "count": 0, "retrievable": 0})
                tally["count"] += 1
                tally["retrievable"] += int(status == "active" and row["valid_from"] <= params[0]
                    and (row["valid_until"] is None or row["valid_until"] > params[1]))
            return Result(list(counts.values()))
        rows.sort(key=lambda row: (row["updated_at"], row["id"]), reverse=True)
        limit, offset = params[-2:]
        return Result(rows[offset:offset + limit])

    def close(self):
        self.closed = True


def install_catalog(monkeypatch, catalog):
    monkeypatch.setattr(memory_view, "resolve_database_config", lambda *args, **kwargs: object())
    monkeypatch.setattr(memory_view, "connect", lambda config: catalog)
    return catalog


def test_personal_overview_filters_other_users_and_projects_and_exposes_evidence(monkeypatch):
    catalog = install_catalog(monkeypatch, ReadOnlyCatalog([
        item(), item("foreign", owner_id="bob", content="PRIVATE"),
        item("project", owner_scope="project", owner_id="shared-repo"),
    ], [{"id": "e1", "memory_id": "preference-1", "source_type": "explicit_user", "source_ref": "message:1",
        "session_id": "web:alice:chat", "excerpt": "Please remember this.", "created_at": NOW,
        "metadata": {"internal": "not exposed"}},
        {"id": "foreign-evidence", "memory_id": "foreign", "source_type": "explicit_user", "excerpt": "PRIVATE"}]))
    result = memory_view.read_memory_overview("alice")
    assert result["available"] is True
    assert [record["id"] for record in result["items"]] == ["preference-1"]
    assert result["counts"] == {"total": 1, "retrievable": 1, "by_status": {"active": 1}}
    assert result["items"][0]["source_types"] == ["explicit_user"]
    assert result["items"][0]["evidence"][0]["excerpt"] == "Please remember this."
    encoded = json.dumps(result)
    assert "PRIVATE" not in encoded and "internal" not in encoded
    assert catalog.closed
    assert catalog.calls[0][0] == "SET TRANSACTION READ ONLY"
    assert all(query.lstrip().startswith(("SELECT", "SET TRANSACTION")) for query, _ in catalog.calls)


def test_all_lifecycle_states_and_validity_are_visible_with_scope_wide_paginated_counts(monkeypatch):
    install_catalog(monkeypatch, ReadOnlyCatalog([
        item("active"), item("candidate", status="candidate"), item("revoked", status="revoked"),
        item("past-active", valid_until=NOW - timedelta(hours=1)),
        item("future-active", valid_from=NOW + timedelta(days=1)),
    ]))
    page = memory_view.read_memory_overview("alice", limit=2)
    assert len(page["items"]) == 2 and page["has_more"] is True and page["next_offset"] == 2
    assert page["counts"] == {"total": 5, "retrievable": 1, "by_status": {"active": 3, "candidate": 1, "revoked": 1}}
    second = memory_view.read_memory_overview("alice", limit=2, offset=2)
    assert second["counts"] == page["counts"]
    assert not ({row["id"] for row in page["items"]} & {row["id"] for row in second["items"]})
    last = memory_view.read_memory_overview("alice", limit=2, offset=4)
    assert last["has_more"] is False and last["next_offset"] is None
    records = {row["id"]: row for row in [*page["items"], *second["items"], *last["items"]]}
    assert records["active"]["retrievable"]
    assert not any(records[key]["retrievable"] for key in ("candidate", "revoked", "past-active", "future-active"))


def test_stored_context_and_verified_bound_task_extend_scope_without_foreign_data(monkeypatch):
    install_catalog(monkeypatch, ReadOnlyCatalog([
        item(), item("task-memory", owner_scope="task", owner_id="coding-123"),
        item("workspace-memory", owner_scope="workspace", owner_id="/workspace/repo"),
        item("project-memory", owner_scope="project", owner_id="repo"),
        item("application-memory", owner_scope="application", owner_id="coding"),
        item("foreign-task", owner_scope="task", owner_id="other-task"),
    ]))
    loaded = []
    def load(storage_id):
        loaded.append(storage_id)
        return {"id": "task:coding-123", "metadata": {"kind": "coding_application", "task_id": "coding-123",
            "parent_session_id": "web:alice:chat", "user_id": "alice", "workspace_root": "/workspace/repo", "repository": "repo"}}
    monkeypatch.setattr(memory_view, "_load_stored_session", load)
    result = memory_view.read_memory_overview("alice", session={"id": "web:alice:chat", "metadata": {"active_coding_task_id": "coding-123"}})
    assert loaded == ["task:coding-123"]
    assert {row["id"] for row in result["items"]} == {"preference-1", "task-memory", "workspace-memory", "project-memory", "application-memory"}
    assert result["context_message"] == ""


@pytest.mark.parametrize("field,value", [("kind", "other"), ("task_id", "other-task"), ("parent_session_id", "web:bob:chat"), ("user_id", "bob")])
def test_invalid_bound_task_never_grants_scopes(monkeypatch, field, value):
    install_catalog(monkeypatch, ReadOnlyCatalog([item(), item("private-task", owner_scope="task", owner_id="coding-123")]))
    metadata = {"kind": "coding_application", "task_id": "coding-123", "parent_session_id": "web:alice:chat", "user_id": "alice"}
    metadata[field] = value
    monkeypatch.setattr(memory_view, "_load_stored_session", lambda storage_id: {"id": "task:coding-123", "metadata": metadata})
    result = memory_view.read_memory_overview("alice", session={"id": "web:alice:chat", "metadata": {"active_coding_task_id": "coding-123"}})
    assert [row["id"] for row in result["items"]] == ["preference-1"]
    assert result["context_message"]


@pytest.mark.parametrize("session", [{"id": "web:bob:chat", "metadata": {}}, {"id": "task:coding-123", "metadata": {}}, {"id": "web:alice:chat", "metadata": {"user_id": "bob"}}])
def test_overview_rejects_foreign_and_internal_parent_sessions_before_database_access(monkeypatch, session):
    calls = []
    monkeypatch.setattr(memory_view, "connect", lambda config: calls.append(config))
    with pytest.raises(ValueError):
        memory_view.read_memory_overview("alice", session=session)
    assert calls == []


@pytest.mark.parametrize("session_id", ["web:bob:chat", "task:coding-123", "telegram:chat", "invalid/slash"])
def test_session_loader_rejects_unowned_identifiers_without_access(monkeypatch, session_id):
    calls = []
    monkeypatch.setattr(memory_view, "_load_stored_session", lambda value: calls.append(value))
    with pytest.raises(ValueError):
        memory_view.load_memory_session(session_id, "alice")
    assert calls == []


def test_session_loader_reads_only_current_user_metadata_and_handles_a_new_chat(monkeypatch):
    catalog = install_catalog(monkeypatch, ReadOnlyCatalog(sessions=[{"id": "web:alice:chat", "metadata": '{"user_id":"alice","active_coding_task_id":"coding-123"}'}]))
    assert memory_view.load_memory_session("chat", "alice")["metadata"]["active_coding_task_id"] == "coding-123"
    assert memory_view.load_memory_session("web:alice:new", "alice") == {"id": "web:alice:new", "metadata": {}}
    assert all(query.startswith(("SELECT id, metadata", "SET TRANSACTION")) for query, _ in catalog.calls)


def test_database_failures_distinguish_unavailable_from_empty_and_hide_credentials(monkeypatch):
    monkeypatch.setattr(memory_view, "resolve_database_config", lambda *args, **kwargs: object())
    def failure(config):
        raise RuntimeError("postgresql://user:SECRET@host/database")
    monkeypatch.setattr(memory_view, "connect", failure)
    result = memory_view.read_memory_overview("alice")
    assert result["available"] is False and result["items"] == [] and result["message"]
    assert "SECRET" not in json.dumps(result)
    with pytest.raises(memory_view.MemoryContextUnavailable) as error:
        memory_view.load_memory_session("chat", "alice")
    assert "SECRET" not in str(error.value)
    install_catalog(monkeypatch, ReadOnlyCatalog())
    empty = memory_view.read_memory_overview("alice")
    assert empty["available"] is True and empty["counts"]["total"] == 0
