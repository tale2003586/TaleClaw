import tempfile
import unittest
from pathlib import Path

from tests.postgres_utils import temporary_postgres_schema
from runtime.db import connect, resolve_database_config, table_columns
from runtime.trace.index_store import TraceIndexStore
from runtime.trace.run_state import RunState


class TraceIndexStoreSchemaTests(unittest.TestCase):
    def test_legacy_trace_tables_gain_safe_additive_columns(self) -> None:
        with temporary_postgres_schema("trace_index_legacy") as dsn:
            import psycopg

            with psycopg.connect(dsn) as conn:
                conn.execute(
                    """
                    CREATE TABLE trace_runs (
                        run_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        trace_path TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO trace_runs (
                        run_id, session_id, trace_path, created_at, updated_at
                    )
                    VALUES (
                        'run_legacy', 'web:legacy', '/tmp/trace.jsonl',
                        '2026-01-01', '2026-01-01'
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE trace_steps (
                        id BIGSERIAL PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        step_index INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        label TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )

            store = TraceIndexStore(dsn)
            reopened = TraceIndexStore(dsn)
            del store, reopened

            with tempfile.TemporaryDirectory() as tmp:
                current = TraceIndexStore(dsn)
                state = RunState.create(
                    session_id="web:current",
                    run_id="run_current",
                    mode="coding",
                )
                current.upsert_run(state, run_dir=Path(tmp))

            config = resolve_database_config(
                dsn,
                env_names=(),
                purpose="trace index test",
            )
            with connect(config) as conn:
                run_columns = table_columns(conn, "trace_runs")
                step_columns = table_columns(conn, "trace_steps")
                legacy_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM trace_runs WHERE run_id = 'run_legacy'"
                ).fetchone()["count"]
                current_mode = conn.execute(
                    "SELECT mode FROM trace_runs WHERE run_id = 'run_current'"
                ).fetchone()["mode"]

            self.assertIn("workspace_requested", run_columns)
            self.assertIn("tool_denials_count", run_columns)
            self.assertIn("reasoning_step", step_columns)
            self.assertEqual(1, legacy_count)
            self.assertEqual("coding", current_mode)


if __name__ == "__main__":
    unittest.main()
