import unittest

from applications.minecraft.models import (
    MinecraftCheckpoint,
    MinecraftTask,
    MinecraftTaskEvent,
    ResourceGoal,
)
from applications.minecraft.stores.memory import StoreConflict
from applications.minecraft.stores.postgres import PostgresMinecraftTaskStore
from tests.postgres_utils import temporary_postgres_schema


class MinecraftPostgresStoreTests(unittest.TestCase):
    def test_crud_events_checkpoint_cancel_and_lease(self):
        with temporary_postgres_schema("minecraft_store") as dsn:
            store = PostgresMinecraftTaskStore(dsn)
            task = MinecraftTask(
                user_id="u",
                session_id="s",
                bot_id="bot",
                goal=ResourceGoal(resource="oak_log", quantity=4),
            )
            created = store.create(task, idempotency_key="same")
            self.assertEqual(created.task_id, store.create(task, idempotency_key="same").task_id)
            updated = store.update(
                created.model_copy(update={"current_count": 1, "version": 1}),
                expected_version=0,
            )
            with self.assertRaises(StoreConflict):
                store.update(updated, expected_version=0)
            event = MinecraftTaskEvent(task_id=task.task_id, event_type="TEST")
            store.append_event(event)
            store.append_event(event)
            self.assertEqual(1, len(store.events(task.task_id)))
            store.save_checkpoint(
                MinecraftCheckpoint(
                    task_id=task.task_id,
                    version=updated.version,
                    plan_version=0,
                    payload={"bot_id": "bot"},
                )
            )
            self.assertEqual("bot", store.latest_checkpoint(task.task_id).payload["bot_id"])
            cancelled = store.request_cancel(task.task_id)
            self.assertTrue(cancelled.cancel_requested)
            self.assertTrue(store.acquire_lease(task.task_id, owner_id="a", ttl_seconds=10))
            self.assertFalse(store.acquire_lease(task.task_id, owner_id="b", ttl_seconds=10))
            self.assertTrue(store.renew_lease(task.task_id, owner_id="a", ttl_seconds=10))
            self.assertTrue(store.release_lease(task.task_id, owner_id="a"))
