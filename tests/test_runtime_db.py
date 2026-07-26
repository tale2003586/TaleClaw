import unittest

from tests.postgres_utils import temporary_postgres_schema
from runtime.db import connect, resolve_database_config, table_columns


class RuntimeDatabaseTests(unittest.TestCase):
    def test_table_columns_reads_the_connection_current_schema(self) -> None:
        with temporary_postgres_schema("runtime_db_columns") as dsn:
            config = resolve_database_config(
                dsn,
                env_names=(),
                purpose="runtime db test",
            )
            with connect(config) as conn:
                conn.execute(
                    "CREATE TABLE schema_probe (local_id TEXT, local_value INTEGER)"
                )
                self.assertEqual(
                    {"local_id", "local_value"},
                    table_columns(conn, "schema_probe"),
                )
                self.assertEqual(set(), table_columns(conn, "missing_table"))


if __name__ == "__main__":
    unittest.main()
