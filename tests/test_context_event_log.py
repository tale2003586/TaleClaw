import unittest

from runtime.context.events import ContextEvent, ContextEventType, payload_checksum
from runtime.sessions.session import Session
from runtime.sessions.session_store import SessionStore


class ContextEventLogTests(unittest.TestCase):
    def test_context_event_is_immutable_and_has_stable_id(self) -> None:
        first = ContextEvent.create(
            session_id="web:event",
            seq=1,
            event_type=ContextEventType.USER_MESSAGE,
            created_at="2026-07-29T00:00:00+00:00",
            payload={"text": "hello", "nested": {"value": 1}},
        )
        second = ContextEvent.create(
            session_id="web:event",
            seq=1,
            event_type="user_message",
            created_at="2026-07-29T00:00:00+00:00",
            payload={"nested": {"value": 1}, "text": "hello"},
        )

        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(first.payload_sha256, payload_checksum(first.payload))
        self.assertEqual("user_message", first.get("event_type"))
        with self.assertRaises(TypeError):
            first.payload["text"] = "changed"  # type: ignore[index]
        with self.assertRaises(TypeError):
            first.payload["nested"]["value"] = 2  # type: ignore[index]

    def test_legacy_messages_backfill_deterministically_without_aliasing_prompt(self) -> None:
        messages = [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ]
        first = Session(id="web:legacy", messages=messages)
        second = Session(id="web:legacy", messages=messages)

        self.assertEqual(
            [event.event_id for event in first.event_log],
            [event.event_id for event in second.event_log],
        )
        self.assertEqual([1, 2], [event.seq for event in first.event_log])
        self.assertEqual([], first.prompt_messages)
        self.assertIsNot(first.messages, first.prompt_messages)
        self.assertEqual(first.event_log, first.active_event_window)

    def test_archive_boundary_keeps_a_tool_transaction_together(self) -> None:
        session = Session(id="web:transaction")
        session.add_message("user", "inspect")
        session.add_message(
            "assistant",
            "",
            tool_calls=[{"id": "call_1", "function": {"name": "read_file"}}],
        )
        session.add_message("tool", "file body", tool_call_id="call_1")
        session.add_message("assistant", "done")

        self.assertEqual(1, session.set_archive_boundary(2))
        self.assertEqual(
            ["tool_call", "tool_result", "assistant_message"],
            [event.type for event in session.active_event_window],
        )
        self.assertEqual([2, 3, 4], [event.seq for event in session.events_after(1)])

    def test_manual_messages_added_by_legacy_callers_are_logged_on_access(self) -> None:
        session = Session(id="web:manual")
        session.messages.append({"role": "user", "content": "raw legacy row"})

        events = session.events_after(0)

        self.assertEqual(1, len(events))
        self.assertEqual("user_message", events[0].type)
        self.assertEqual("raw legacy row", events[0].payload["message"]["content"])

    def test_postgres_restart_preserves_events_and_checkpoint_boundary(self) -> None:
        try:
            from tests.postgres_utils import temporary_postgres_schema
            with temporary_postgres_schema("context_event_log") as dsn:
                store = SessionStore(dsn)
                session = Session(id="web:restart")
                session.add_message("user", "inspect")
                session.add_message("assistant", "done")
                original_event_ids = [event.event_id for event in session.event_log]
                store.save_session(session)

                checkpoint = store.compact_session(
                    session,
                    checkpoint={"objective": "inspect", "version": 1},
                    archive_boundary_seq=1,
                )
                store.close()

                reopened = SessionStore(dsn)
                loaded = reopened.load_session("web:restart")
                reopened.close()
        except Exception as exc:
            if exc.__class__.__name__ in {"OperationalError", "ImportError"}:
                self.skipTest(f"PostgreSQL unavailable: {exc.__class__.__name__}")
            raise

        self.assertEqual(original_event_ids, [event.event_id for event in loaded["event_log"][:2]])
        self.assertEqual(1, loaded["archive_boundary_seq"])
        self.assertEqual(checkpoint["checkpoint_id"], loaded["checkpoints"][0]["checkpoint_id"])
        self.assertEqual(
            payload_checksum(checkpoint["state"]),
            checkpoint["state_sha256"],
        )
        self.assertEqual(
            payload_checksum(loaded["checkpoints"][0]["state"]),
            loaded["checkpoints"][0]["state_sha256"],
        )
        self.assertEqual(
            checkpoint["state_sha256"],
            loaded["checkpoints"][0]["state_sha256"],
        )
        self.assertEqual("compaction_completed", loaded["event_log"][-1].type)


if __name__ == "__main__":
    unittest.main()
