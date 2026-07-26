import asyncio

from applications.minecraft.models import MinecraftTask, ResourceGoal
from applications.minecraft.stores.memory import InMemoryMinecraftTaskStore
from applications.minecraft.worker import MinecraftWorker
from runtime.cancellation import CancellationRegistry

from .fakes import FakeBridge


def test_only_lease_owner_can_submit_actions():
    store = InMemoryMinecraftTaskStore()
    task = store.create(
        MinecraftTask(
            user_id="u",
            session_id="s",
            bot_id="bot",
            goal=ResourceGoal(resource="oak_log", quantity=1),
            cancellation_scope="minecraft:lease",
        ),
        idempotency_key="lease-task",
    )
    assert store.acquire_lease(task.task_id, owner_id="other", ttl_seconds=30)
    bridge = FakeBridge()
    worker = MinecraftWorker(
        store=store,
        bridge=bridge,
        cancellations=CancellationRegistry(),
        owner_id="candidate",
    )
    result = asyncio.run(worker.run(task.task_id))
    assert result.stages == ("lease_denied",)
    assert bridge.actions == []
