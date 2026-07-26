import unittest

from memory.archive_store import MemoryArchiveStore
from tests.postgres_utils import temporary_postgres_schema
from runtime.db import connect, resolve_database_config, table_columns
from runtime.tooling.result_store import PostgresToolResultStore
from runtime.trace.index_store import TraceIndexStore
from web.auth_store import WebAuthStore


def _columns(dsn: str, table: str) -> set[str]:
    config = resolve_database_config(dsn, env_names=(), purpose="schema contract test")
    with connect(config) as conn:
        return table_columns(conn, table)


def _indexes(dsn: str, table: str) -> set[str]:
    config = resolve_database_config(dsn, env_names=(), purpose="schema contract test")
    with connect(config) as conn:
        rows = conn.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = %s
            """,
            (table,),
        ).fetchall()
    return {str(row["indexname"]) for row in rows}


class PostgresStoreSchemaContractTests(unittest.TestCase):
    def test_web_auth_schema_is_complete_and_idempotent(self) -> None:
        with temporary_postgres_schema("web_auth_contract") as dsn:
            WebAuthStore(dsn)
            WebAuthStore(dsn)

            self.assertEqual(
                {
                    "user_id",
                    "password_hash",
                    "salt",
                    "role",
                    "source",
                    "created_at",
                    "updated_at",
                },
                _columns(dsn, "web_users"),
            )
            self.assertEqual(
                {"token_hash", "user_id", "created_at", "expires_at"},
                _columns(dsn, "web_auth_sessions"),
            )
            self.assertIn(
                "idx_web_auth_sessions_expiry",
                _indexes(dsn, "web_auth_sessions"),
            )

    def test_memory_archive_schema_is_complete_and_idempotent(self) -> None:
        with temporary_postgres_schema("memory_archive_contract") as dsn:
            MemoryArchiveStore(dsn)
            MemoryArchiveStore(dsn)

            self.assertEqual(
                {
                    "id",
                    "session_id",
                    "mode",
                    "user_text",
                    "assistant_summary",
                    "source_ref",
                    "created_at",
                    "archived_at",
                    "metadata",
                },
                _columns(dsn, "memory_archive"),
            )
            self.assertIn(
                "idx_memory_archive_session_created",
                _indexes(dsn, "memory_archive"),
            )

    def test_trace_schema_is_complete_and_idempotent(self) -> None:
        with temporary_postgres_schema("trace_contract") as dsn:
            TraceIndexStore(dsn)
            TraceIndexStore(dsn)

            self.assertEqual(
                {
                    "run_id",
                    "session_id",
                    "channel",
                    "chat_id",
                    "user_id",
                    "user_role",
                    "mode",
                    "execution_path",
                    "status",
                    "stop_reason",
                    "failure_category",
                    "failure_reason",
                    "workspace_root",
                    "workspace_requested",
                    "workspace_allowed_root",
                    "trace_path",
                    "report_path",
                    "summary_path",
                    "metrics_path",
                    "reasoning_steps",
                    "model_calls_count",
                    "tool_calls_count",
                    "tool_failures_count",
                    "tool_denials_count",
                    "total_tokens",
                    "duration_ms",
                    "last_tool",
                    "started_at",
                    "finished_at",
                    "metadata",
                    "created_at",
                    "updated_at",
                },
                _columns(dsn, "trace_runs"),
            )
            self.assertEqual(
                {
                    "id",
                    "run_id",
                    "step_index",
                    "kind",
                    "reasoning_step",
                    "label",
                    "status",
                    "detail",
                    "duration_ms",
                    "created_at",
                },
                _columns(dsn, "trace_steps"),
            )
            self.assertTrue(
                {
                    "idx_trace_runs_started",
                    "idx_trace_runs_status",
                }.issubset(_indexes(dsn, "trace_runs"))
            )
            self.assertIn("idx_trace_steps_run", _indexes(dsn, "trace_steps"))

    def test_postgres_tool_result_schema_and_round_trip(self) -> None:
        with temporary_postgres_schema("tool_result_contract") as dsn:
            first = PostgresToolResultStore(dsn)
            second = PostgresToolResultStore(dsn)
            del second

            stored = first.put(
                session_id="web:test",
                call_id="call_1",
                tool_name="read_file",
                arguments={"path": "README.md"},
                content="preserved tool output",
                status="success",
            )
            content, metadata = first.get(stored.result_id)

            self.assertEqual("preserved tool output", content)
            self.assertEqual("read_file", metadata["tool_name"])
            self.assertEqual(
                {
                    "result_id",
                    "session_id",
                    "call_id",
                    "tool_name",
                    "arguments_json",
                    "status",
                    "content",
                    "content_sha256",
                    "created_at",
                    "metadata_json",
                },
                _columns(dsn, "tool_results"),
            )
            self.assertIn(
                "idx_tool_results_session",
                _indexes(dsn, "tool_results"),
            )


if __name__ == "__main__":
    unittest.main()
