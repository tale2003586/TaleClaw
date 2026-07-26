import unittest
from datetime import datetime, timezone

from runtime.sessions.session import Session, SessionManager
from runtime.sessions.session_store import SessionStore
from tests.postgres_utils import temporary_postgres_schema


class SessionStoreIncrementalTests(unittest.TestCase):
    def test_initialization_completes_legacy_agent_identity_migration(self) -> None:
        with temporary_postgres_schema("session_store_legacy") as dsn:
            import psycopg

            with psycopg.connect(dsn) as conn:
                conn.execute(
                    """
                    CREATE TABLE sessions (
                        id             TEXT PRIMARY KEY,
                        current_mode   TEXT NOT NULL,
                        created_at     TEXT NOT NULL,
                        updated_at     TEXT NOT NULL,
                        last_compacted TEXT,
                        metadata       TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE messages (
                        session_id   TEXT NOT NULL,
                        seq          INTEGER NOT NULL,
                        role         TEXT NOT NULL,
                        timestamp    TEXT,
                        message_json TEXT NOT NULL,
                        PRIMARY KEY (session_id, seq),
                        FOREIGN KEY (session_id) REFERENCES sessions(id)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO sessions (
                        id, current_mode, created_at, updated_at, metadata
                    )
                    VALUES (
                        'web:legacy', 'coding', '2026-01-01', '2026-01-01', '{}'
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO messages (
                        session_id, seq, role, timestamp, message_json
                    )
                    VALUES (
                        'web:legacy', 0, 'user', '2026-01-01',
                        '{"role":"user","content":"preserved"}'
                    )
                    """
                )

            store = SessionStore(dsn)
            loaded = store.load_session("web:legacy")
            self.assertEqual("coding", loaded["active_agent"])

            new_session = Session(id="web:new", active_agent="hybrid")
            store.save_session(new_session)
            store.close()

            reopened = SessionStore(dsn)
            self.assertEqual(
                "preserved",
                reopened.load_session("web:legacy")["messages"][0]["content"],
            )
            reopened.close()

            with psycopg.connect(dsn) as conn:
                columns = {
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'sessions'
                        """
                    ).fetchall()
                }
            self.assertIn("active_agent", columns)
            self.assertNotIn("current_mode", columns)

    def test_half_migrated_session_prefers_existing_active_agent(self) -> None:
        with temporary_postgres_schema("session_store_half_migrated") as dsn:
            import psycopg

            with psycopg.connect(dsn) as conn:
                conn.execute(
                    """
                    CREATE TABLE sessions (
                        id             TEXT PRIMARY KEY,
                        current_mode   TEXT NOT NULL,
                        active_agent   TEXT,
                        created_at     TEXT NOT NULL,
                        updated_at     TEXT NOT NULL,
                        last_compacted TEXT,
                        metadata       TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO sessions (
                        id, current_mode, active_agent, created_at, updated_at
                    )
                    VALUES
                        ('web:target-wins', 'coding', 'bot', '2026-01-01', '2026-01-01'),
                        ('web:legacy-fills', 'coding', NULL, '2026-01-01', '2026-01-01'),
                        ('web:default-fills', '', '', '2026-01-01', '2026-01-01')
                    """
                )

            store = SessionStore(dsn)
            try:
                self.assertEqual(
                    "bot",
                    store.load_session("web:target-wins")["active_agent"],
                )
                self.assertEqual(
                    "coding",
                    store.load_session("web:legacy-fills")["active_agent"],
                )
                self.assertEqual(
                    "hybrid",
                    store.load_session("web:default-fills")["active_agent"],
                )
            finally:
                store.close()

            reopened = SessionStore(dsn)
            reopened.close()

            with psycopg.connect(dsn) as conn:
                columns = {
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'sessions'
                        """
                    ).fetchall()
                }
            self.assertIn("active_agent", columns)
            self.assertNotIn("current_mode", columns)

    def test_failed_legacy_migration_rolls_back_all_schema_changes(self) -> None:
        with temporary_postgres_schema("session_store_rollback") as dsn:
            import psycopg

            with psycopg.connect(dsn) as conn:
                conn.execute(
                    """
                    CREATE TABLE sessions (
                        id             TEXT PRIMARY KEY,
                        current_mode   TEXT NOT NULL,
                        created_at     TEXT NOT NULL,
                        updated_at     TEXT NOT NULL,
                        last_compacted TEXT,
                        metadata       TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO sessions (
                        id, current_mode, created_at, updated_at
                    )
                    VALUES ('web:legacy', 'coding', '2026-01-01', '2026-01-01')
                    """
                )
                conn.execute(
                    """
                    CREATE VIEW legacy_session_modes AS
                    SELECT id, current_mode FROM sessions
                    """
                )

            with self.assertRaises(Exception):
                SessionStore(dsn)

            with psycopg.connect(dsn) as conn:
                columns = {
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'sessions'
                        """
                    ).fetchall()
                }
                row = conn.execute(
                    "SELECT current_mode FROM sessions WHERE id = 'web:legacy'"
                ).fetchone()

            self.assertIn("current_mode", columns)
            self.assertNotIn("active_agent", columns)
            self.assertEqual("coding", row[0])

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
