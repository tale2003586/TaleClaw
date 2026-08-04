from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from applications.coding.context_state import build_coding_context_view
from applications.coding.task_state import ensure_task_state, load_task_state
from runtime.context import ArtifactStore, LongContentDetector
from runtime.context.events import ContextEvent
from runtime.sessions.session import Session, SessionManager
from runtime.sessions.session_store import SessionStore


class _MemorySessionStore:
    def __init__(self, loaded: dict) -> None:
        self.loaded = deepcopy(loaded)
        self.save_count = 0
        self.compact_count = 0

    def load_session(self, session_id: str):
        if self.loaded.get("id") != session_id:
            return None
        return deepcopy(self.loaded)

    def save_session(self, session: Session) -> None:
        self.save_count += 1
        self.loaded = {
            "id": session.id,
            "messages": deepcopy(session.messages),
            "active_agent": session.active_agent,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "last_compacted": session.last_compacted,
            "metadata": deepcopy(session.metadata),
            "event_log": [event.to_dict() for event in session.event_log],
            "archive_boundary_seq": session.archive_boundary_seq,
            "checkpoints": deepcopy(session.checkpoints),
        }

    def compact_session(
        self,
        session: Session,
        *,
        checkpoint: dict,
        archive_boundary_seq: int | None = None,
        metadata: dict | None = None,
    ) -> dict:
        self.compact_count += 1
        created_at = "2026-07-29T00:00:00+00:00"
        state_json = json.dumps(
            checkpoint,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        state_sha256 = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
        checkpoint_id = f"memory-checkpoint-{self.compact_count}"
        boundary = max(0, int(archive_boundary_seq or 0))
        checkpoint_event = session.append_event(
            "task_state_checkpoint",
            {"checkpoint_id": checkpoint_id, "state_sha256": state_sha256},
            created_at=created_at,
        )
        completion_event = session.append_event(
            "compaction_completed",
            {
                "checkpoint_id": checkpoint_id,
                "checkpoint_event_id": checkpoint_event.event_id,
            },
            created_at=created_at,
        )
        record = {
            "checkpoint_id": checkpoint_id,
            "archive_boundary_seq": boundary,
            "completion_event_id": completion_event.event_id,
            "created_at": created_at,
            "state": deepcopy(checkpoint),
            "state_sha256": state_sha256,
            "metadata": deepcopy(metadata or {}),
        }
        session.checkpoints.insert(0, record)
        self.save_session(session)
        return record

    def close(self) -> None:
        return None


def _legacy_row(*, session_id: str, content: str) -> dict:
    return {
        "id": session_id,
        "messages": [{"role": "user", "content": content}],
        "active_agent": "coding",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "last_compacted": None,
        "metadata": {"legacy": True},
        "event_log": [],
        "archive_boundary_seq": 0,
        "checkpoints": [],
    }


def test_loaded_legacy_long_message_is_externalized_once_before_event_backfill(
    tmp_path: Path,
) -> None:
    original = "Inspect this payload.\n\n" + ("unique legacy source line\n" * 200)
    artifact_store = ArtifactStore(tmp_path / "artifacts")
    detector = LongContentDetector(
        artifact_store,
        max_tokens=20,
        max_chars=80,
        max_bytes=160,
    )
    store = _MemorySessionStore(_legacy_row(session_id="web:legacy-long", content=original))
    store.loaded["metadata"] = {"working_memory": {"objective": original}}

    with patch("runtime.sessions.session.SessionStore", return_value=store):
        manager = SessionManager("memory://unused", long_content_detector=detector)
        session = manager.get_or_create("web:legacy-long")

        assert store.save_count == 1
        assert [event.type for event in session.event_log] == [
            "user_message",
            "artifact_created",
        ]
        message_event_id = session.event_log[0].event_id
        message = session.messages[0]
        artifact_ref = message["metadata"]["artifact_ref"]
        assert "artifact_ref" not in message
        assert artifact_store.read_artifact(artifact_ref["artifact_id"]) == original
        message_event_ref = session.event_log[0].payload["message"]["metadata"][
            "artifact_ref"
        ]
        assert message_event_ref["artifact_id"] == artifact_ref["artifact_id"]
        assert (
            session.event_log[1].payload["artifact_ref"]["artifact_id"]
            == artifact_ref["artifact_id"]
        )

        ensure_task_state(session, objective_summary="fallback")

        assert store.compact_count == 1
        assert store.save_count == 2
        persisted_context = json.dumps(
            {
                "messages": store.loaded["messages"],
                "events": store.loaded["event_log"],
                "metadata": store.loaded["metadata"],
                "checkpoints": store.loaded["checkpoints"],
            },
            ensure_ascii=False,
        )
        assert original not in persisted_context
        assert len(list((tmp_path / "artifacts" / "content").iterdir())) == 1
        assert len(list((tmp_path / "artifacts" / "metadata").iterdir())) == 1

        manager._sessions.clear()
        reloaded = manager.get_or_create("web:legacy-long")

        assert store.save_count == 2
        assert len(reloaded.event_log) == 4
        assert sum(event.type == "artifact_created" for event in reloaded.event_log) == 1
        assert reloaded.event_log[0].event_id == message_event_id
        assert reloaded.messages[0]["metadata"]["artifact_ref"] == artifact_ref
        assert artifact_store.read_artifact(artifact_ref["artifact_id"]) == original
        assert len(list((tmp_path / "artifacts" / "content").iterdir())) == 1
        manager.close()


def test_legacy_top_level_artifact_ref_is_normalized_without_duplicate_fact(
    tmp_path: Path,
) -> None:
    artifact_store = ArtifactStore(tmp_path / "artifacts")
    ref = artifact_store.put_artifact("original legacy source", artifact_type="text")
    detector = LongContentDetector(
        artifact_store,
        max_tokens=100,
        max_chars=500,
        max_bytes=1_000,
    )
    loaded = _legacy_row(
        session_id="web:legacy-top-level-ref",
        content="Source content is available by artifact reference.",
    )
    loaded["messages"][0]["artifact_ref"] = ref.to_dict()
    store = _MemorySessionStore(loaded)

    with patch("runtime.sessions.session.SessionStore", return_value=store):
        manager = SessionManager("memory://unused", long_content_detector=detector)
        session = manager.get_or_create("web:legacy-top-level-ref")

        assert "artifact_ref" not in session.messages[0]
        assert session.messages[0]["metadata"]["artifact_ref"] == ref.to_dict()
        assert [event.type for event in session.event_log] == [
            "user_message",
            "artifact_created",
        ]

        manager._sessions.clear()
        reloaded = manager.get_or_create("web:legacy-top-level-ref")

        assert store.save_count == 1
        assert sum(event.type == "artifact_created" for event in reloaded.event_log) == 1
        assert reloaded.messages[0]["metadata"]["artifact_ref"] == ref.to_dict()
        manager.close()


def test_tracked_long_audit_event_is_replaced_in_active_prompt_without_mutation(
    tmp_path: Path,
) -> None:
    session_id = "web:tracked-legacy-long"
    timestamp = "2026-01-01T00:00:00+00:00"
    original = "Keep the audit fact.\n\n" + ("private historical payload\n" * 200)
    raw_message = {"role": "user", "content": original, "timestamp": timestamp}
    raw_event = ContextEvent.create(
        session_id=session_id,
        seq=1,
        event_type="user_message",
        payload={"message": raw_message},
        created_at=timestamp,
    )
    loaded = _legacy_row(session_id=session_id, content=original)
    loaded["messages"] = [raw_message]
    loaded["event_log"] = [raw_event.to_dict()]
    store = _MemorySessionStore(loaded)
    artifact_store = ArtifactStore(tmp_path / "artifacts")
    detector = LongContentDetector(
        artifact_store,
        max_tokens=20,
        max_chars=80,
        max_bytes=160,
    )

    with patch("runtime.sessions.session.SessionStore", return_value=store):
        manager = SessionManager("memory://unused", long_content_detector=detector)
        session = manager.get_or_create(session_id)

        assert [event.type for event in session.event_log] == [
            "user_message",
            "user_message",
            "artifact_created",
            "legacy_message_replaced",
        ]
        assert session.event_log[0].event_id == raw_event.event_id
        assert session.event_log[0].payload["message"]["content"] == original
        replacement = session.event_log[-1]
        assert replacement.payload["replaces_event_id"] == raw_event.event_id
        assert replacement.payload["replacement_event_id"] == session.event_log[1].event_id
        assert raw_event.event_id not in {
            event.event_id for event in session.active_event_window
        }
        assert raw_event.event_id not in {event.event_id for event in session.events_after(0)}
        assert original not in json.dumps(session.messages, ensure_ascii=False)
        assert original not in json.dumps(
            [event.to_dict() for event in session.active_event_window],
            ensure_ascii=False,
        )

        view = build_coding_context_view(
            session=session,
            objective=session.messages[0]["content"],
            active_turn_start_index=0,
            static_messages=[],
            usable_input_tokens=10_000,
        )
        prompt_json = json.dumps(
            {
                "state": view.state_message,
                "recent": view.recent_messages,
                "active": view.active_messages,
            },
            ensure_ascii=False,
        )
        assert original not in prompt_json
        assert session.checkpoints[0]["metadata"]["migration"]["source"] == "session_history"
        assert store.save_count == 2

        manager._sessions.clear()
        reloaded = manager.get_or_create(session_id)

        assert store.save_count == 2
        assert sum(event.type == "artifact_created" for event in reloaded.event_log) == 1
        assert sum(
            event.type == "legacy_message_replaced" for event in reloaded.event_log
        ) == 1
        assert reloaded.event_log[0].event_id == raw_event.event_id
        assert raw_event.event_id not in {
            event.event_id for event in reloaded.active_event_window
        }
        manager.close()


@pytest.mark.parametrize(
    ("source", "legacy_payload"),
    [
        (
            "working_memory",
            {
                "objective": "legacy working objective " * 100,
                "pending_units": [{"unit_id": "next", "description": "continue"}],
            },
        ),
        (
            "coding_context_state",
            {
                "objective": "legacy coding context objective " * 100,
                "pending_actions": [{"id": "next", "description": "continue"}],
            },
        ),
    ],
)
def test_legacy_task_state_migration_checkpoint_is_idempotent_and_recoverable(
    source: str,
    legacy_payload: dict,
) -> None:
    session_id = f"task:migrate-{source}"
    loaded = _legacy_row(session_id=session_id, content="short legacy request")
    loaded["messages"] = []
    loaded["metadata"] = {source: legacy_payload}
    store = _MemorySessionStore(loaded)
    original_body = legacy_payload["objective"]

    with patch("runtime.sessions.session.SessionStore", return_value=store):
        manager = SessionManager("memory://unused")
        session = manager.get_or_create(session_id)
        state = ensure_task_state(session, objective_summary="fallback")
        event_count = len(session.event_log)
        checkpoint_count = len(session.checkpoints)
        repeated = ensure_task_state(session, objective_summary="ignored")

        assert repeated.to_dict() == state.to_dict()
        assert checkpoint_count == 1
        assert len(session.checkpoints) == checkpoint_count
        assert len(session.event_log) == event_count
        assert store.compact_count == 1
        checkpoint = session.checkpoints[0]
        migration = checkpoint["metadata"]["migration"]
        assert migration["kind"] == "legacy_task_state_migration"
        assert migration["source"] == source
        assert len(migration["source_sha256"]) == 64
        assert len(migration["task_state_sha256"]) == 64
        assert source not in session.metadata
        assert original_body not in json.dumps(
            {"metadata": store.loaded["metadata"], "checkpoint": checkpoint},
            ensure_ascii=False,
        )

        manager._sessions.clear()
        recovered_session = manager.get_or_create(session_id)
        recovered_session.metadata = {}
        recovered = load_task_state(recovered_session)
        manager.close()

    assert recovered is not None
    assert recovered.to_dict() == state.to_dict()


def test_history_only_session_creates_one_initial_migration_checkpoint() -> None:
    session_id = "task:history-only-migration"
    loaded = _legacy_row(session_id=session_id, content="older objective")
    loaded["messages"] = [
        {"role": "user", "content": "older objective"},
        {"role": "assistant", "content": "older answer"},
        {"role": "user", "content": "latest real objective"},
        {
            "role": "user",
            "content": "runtime synthetic objective",
            "metadata": {"source": "runtime-generated:test"},
        },
    ]
    loaded["metadata"] = {}
    store = _MemorySessionStore(loaded)

    with patch("runtime.sessions.session.SessionStore", return_value=store):
        manager = SessionManager("memory://unused")
        session = manager.get_or_create(session_id)

        state = ensure_task_state(session, objective_summary="fallback objective")
        repeated = ensure_task_state(session, objective_summary="ignored")

        assert state.objective.summary == "latest real objective"
        assert repeated.to_dict() == state.to_dict()
        assert store.compact_count == 1
        assert len(session.checkpoints) == 1
        migration = session.checkpoints[0]["metadata"]["migration"]
        assert migration["source"] == "session_history"
        assert migration["source_info"]["message_index"] == 2
        assert migration["source_info"]["event_ref"].startswith("event://")

        manager._sessions.clear()
        recovered_session = manager.get_or_create(session_id)
        recovered_session.metadata = {}
        recovered = load_task_state(recovered_session)
        manager.close()

    assert recovered is not None
    assert recovered.to_dict() == state.to_dict()


def test_history_only_checkpoint_failure_restores_pre_migration_session() -> None:
    session = Session(
        id="task:history-migration-retry",
        messages=[{"role": "user", "content": "history goal"}],
    )
    original_event_ids = [event.event_id for event in session.event_log]

    def fail_checkpoint(**_kwargs):
        raise OSError("history checkpoint unavailable")

    with pytest.raises(OSError, match="history checkpoint unavailable"):
        ensure_task_state(
            session,
            objective_summary="fallback",
            checkpoint_persister=fail_checkpoint,
        )

    assert session.metadata == {}
    assert session.checkpoints == []
    assert [event.event_id for event in session.event_log] == original_event_ids

    state = ensure_task_state(session, objective_summary="fallback")
    assert state.objective.summary == "history goal"
    assert len(session.checkpoints) == 1


def test_failed_migration_checkpoint_does_not_hide_legacy_source() -> None:
    legacy = {"objective": "retry this migration", "pending_units": []}
    session = Session(id="task:migration-retry", metadata={"working_memory": legacy})

    def fail_checkpoint(**_kwargs):
        raise OSError("checkpoint unavailable")

    with pytest.raises(OSError, match="checkpoint unavailable"):
        ensure_task_state(
            session,
            objective_summary="fallback",
            checkpoint_persister=fail_checkpoint,
        )

    assert session.metadata == {"working_memory": legacy}
    assert session.event_log == []
    assert session.checkpoints == []

    state = ensure_task_state(session, objective_summary="fallback")
    assert state.objective.summary == "retry this migration"
    assert len(session.checkpoints) == 1


def test_postgres_migration_checkpoint_survives_restart() -> None:
    try:
        from tests.postgres_utils import temporary_postgres_schema

        with temporary_postgres_schema("legacy_task_state_checkpoint") as dsn:
            seed = SessionManager(dsn)
            legacy = Session(
                id="task:legacy-checkpoint",
                metadata={"working_memory": {"objective": "recover after restart"}},
            )
            seed.save(legacy)
            seed.close()

            manager = SessionManager(dsn)
            migrated = manager.get_or_create("task:legacy-checkpoint")
            expected = ensure_task_state(migrated, objective_summary="fallback").to_dict()
            assert len(migrated.checkpoints) == 1
            manager.close()

            reopened = SessionStore(dsn)
            loaded = reopened.load_session("task:legacy-checkpoint")
            reopened.close()
    except Exception as exc:
        if exc.__class__.__name__ in {"OperationalError", "ImportError"}:
            pytest.skip(f"PostgreSQL unavailable: {exc.__class__.__name__}")
        raise

    assert loaded is not None
    assert len(loaded["checkpoints"]) == 1
    assert loaded["checkpoints"][0]["metadata"]["migration"]["source"] == "working_memory"
    recovered = Session(
        id=loaded["id"],
        metadata={},
        event_log=loaded["event_log"],
        checkpoints=loaded["checkpoints"],
    )
    assert load_task_state(recovered).to_dict() == expected
