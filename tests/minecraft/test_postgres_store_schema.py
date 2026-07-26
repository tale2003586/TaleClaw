import unittest

from applications.minecraft.stores.postgres import PostgresMinecraftTaskStore
from runtime.db import connect, resolve_database_config, table_columns
from tests.postgres_utils import temporary_postgres_schema


class MinecraftPostgresSchemaTests(unittest.TestCase):
    def test_schema_is_complete_and_idempotent(self):
        with temporary_postgres_schema("minecraft_contract") as dsn:
            PostgresMinecraftTaskStore(dsn)
            PostgresMinecraftTaskStore(dsn)
            config = resolve_database_config(dsn, env_names=(), purpose="test")
            with connect(config) as conn:
                self.assertEqual(
                    {
                        "task_id",
                        "idempotency_key",
                        "bot_id",
                        "status",
                        "version",
                        "cancel_requested",
                        "lease_owner",
                        "lease_expires_at",
                        "task_json",
                        "created_at",
                        "updated_at",
                    },
                    table_columns(conn, "minecraft_tasks"),
                )
                for table in (
                    "minecraft_runs",
                    "minecraft_task_events",
                    "minecraft_checkpoints",
                ):
                    self.assertTrue(table_columns(conn, table))
