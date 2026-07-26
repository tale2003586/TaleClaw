import asyncio

from applications.minecraft.models import (
    MinecraftCheckpoint,
    MinecraftTask,
    ResourceGoal,
    TaskStatus,
)
from applications.minecraft.service import MinecraftTaskService
from applications.minecraft.stores.memory import InMemoryMinecraftTaskStore
from applications.minecraft.worker import MinecraftWorker
from runtime.cancellation import CancellationRegistry

from .fakes import FakeBridge


def _service(store, bridge):
    cancellations = CancellationRegistry()
    worker = MinecraftWorker(
        store=store,
        bridge=bridge,
        cancellations=cancellations,
    )
    return MinecraftTaskService(
        store=store,
        bridge=bridge,
        cancellations=cancellations,
        worker=worker,
    )


def test_resume_blocks_changed_world_identity():
    store = InMemoryMinecraftTaskStore()
    task = store.create(
        MinecraftTask(
            user_id="u",
            session_id="s",
            bot_id="test-bot",
            goal=ResourceGoal(resource="oak_log", quantity=1),
            status=TaskStatus.OBSERVING,
            cancellation_scope="minecraft:resume",
        ),
        idempotency_key="resume-mismatch",
    )
    store.save_checkpoint(
        MinecraftCheckpoint(
            task_id=task.task_id,
            version=0,
            plan_version=0,
            payload={
                "bot_id": "test-bot",
                "server_id": "test-server",
                "world_id": "different-world",
                "baseline_count": 0,
            },
        )
    )
    recovered = asyncio.run(_service(store, FakeBridge()).resume_recoverable())
    assert recovered[0].status is TaskStatus.BLOCKED
    assert recovered[0].error_code == "resume_identity_mismatch"


def test_resume_matching_checkpoint_continues_from_current_inventory():
    store = InMemoryMinecraftTaskStore()
    task = store.create(
        MinecraftTask(
            user_id="u",
            session_id="s",
            bot_id="test-bot",
            goal=ResourceGoal(resource="oak_log", quantity=1),
            status=TaskStatus.OBSERVING,
            cancellation_scope="minecraft:resume-ok",
        ),
        idempotency_key="resume-match",
    )
    store.save_checkpoint(
        MinecraftCheckpoint(
            task_id=task.task_id,
            version=0,
            plan_version=0,
            payload={
                "bot_id": "test-bot",
                "server_id": "test-server",
                "world_id": "test-world",
                "baseline_count": 0,
            },
        )
    )
    service = _service(store, FakeBridge(inventory={"oak_log": 1}))

    async def scenario():
        recovered = await service.resume_recoverable()
        await asyncio.gather(*service._background.values())
        return recovered, store.get(task.task_id)

    recovered, final = asyncio.run(scenario())
    assert recovered[0].task_id == task.task_id
    assert final.status is TaskStatus.SUCCEEDED
    assert final.net_acquired == 1
