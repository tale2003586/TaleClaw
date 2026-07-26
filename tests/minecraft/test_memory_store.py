import pytest

from applications.minecraft.models import MinecraftTask, ResourceGoal, TaskStatus
from applications.minecraft.stores.memory import (
    InMemoryMinecraftTaskStore,
    StoreConflict,
)


def _task(bot="bot"):
    return MinecraftTask(
        user_id="user",
        session_id="session",
        bot_id=bot,
        goal=ResourceGoal(resource="oak_log", quantity=4),
    )


def test_create_is_idempotent_and_single_active_per_bot():
    store = InMemoryMinecraftTaskStore()
    task = _task()
    first = store.create(task, idempotency_key="same-key")
    assert store.create(_task(), idempotency_key="same-key").task_id == first.task_id
    with pytest.raises(StoreConflict):
        store.create(_task(), idempotency_key="other-key")


def test_update_uses_optimistic_version():
    store = InMemoryMinecraftTaskStore()
    task = store.create(_task(), idempotency_key="key")
    changed = task.model_copy(update={"status": TaskStatus.CONNECTING, "version": 1})
    assert store.update(changed, expected_version=0).version == 1
    with pytest.raises(StoreConflict):
        store.update(changed, expected_version=0)
