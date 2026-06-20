import unittest
from datetime import datetime, timezone

from sessions.session import Session, SessionManager
from sessions.session_store import SessionStore
from postgres_utils import temporary_postgres_schema


class SessionStoreIncrementalTests(unittest.TestCase):
    def test_save_session_inserts_only_changed_suffix(self) -> None:
        with temporary_postgres_schema("session_store") as dsn:
            store = SessionStore(dsn)
            session = Session(id="web:test")
            session.add_message("user", "hello")
            session.add_message("assistant", "hi")

            store.save_session(session)
            self.assertEqual(2, store.last_message_insert_count)

            store.save_session(session)
            self.assertEqual(0, store.last_message_insert_count)

            session.add_message("user", "next")
            store.save_session(session)
            self.assertEqual(1, store.last_message_insert_count)

            loaded = store.load_session("web:test")
            self.assertEqual(["hello", "hi", "next"], [m["content"] for m in loaded["messages"]])
            store.close()

    def test_session_manager_evicts_oldest_cached_session(self) -> None:
        with temporary_postgres_schema("session_manager") as dsn:
            manager = SessionManager(dsn, max_sessions=2)

            first = manager.get_or_create("web:first")
            manager.get_or_create("web:second")
            manager.get_or_create("web:third")

            self.assertNotIn("web:first", manager._sessions)
            self.assertIn("web:second", manager._sessions)
            self.assertIn("web:third", manager._sessions)
            self.assertIsNot(first, manager.get_or_create("web:first"))
            manager.close()

    def test_session_manager_can_cleanup_expired_sessions(self) -> None:
        with temporary_postgres_schema("session_cleanup") as dsn:
            manager = SessionManager(dsn)
            old = Session(id="web:old")
            old.updated_at = "2026-01-01T00:00:00+00:00"
            fresh = Session(id="web:fresh")
            fresh.updated_at = "2026-06-15T00:00:00+00:00"
            manager._store.save_session(old)
            manager._store.save_session(fresh)

            removed = manager.cleanup_expired_sessions(
                max_age_days=30,
                now=datetime(2026, 6, 16, tzinfo=timezone.utc),
            )

            self.assertEqual(1, removed)
            self.assertIsNone(manager._store.load_session("web:old"))
            self.assertIsNotNone(manager._store.load_session("web:fresh"))
            manager.close()


if __name__ == "__main__":
    unittest.main()
