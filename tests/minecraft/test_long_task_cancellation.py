import asyncio

from applications.minecraft.models import ResourceGoal, TaskStatus
from applications.minecraft.service import MinecraftTaskService
from applications.minecraft.stores.memory import InMemoryMinecraftTaskStore
from applications.minecraft.worker import MinecraftWorker
from runtime.cancellation import CancellationRegistry

from .fakes import FakeBridge


class PausingBridge(FakeBridge):
    def __init__(self):
        super().__init__()
        self.action_started = asyncio.Event()
        self.release_action = asyncio.Event()

    async def watch_action(self, action_id, cancellation_token):
        self.action_started.set()
        await self.release_action.wait()
        async for event in super().watch_action(action_id, cancellation_token):
            yield event


def test_persisted_cancel_stops_all_followup_actions():
    async def scenario():
        store = InMemoryMinecraftTaskStore()
        bridge = PausingBridge()
        cancellations = CancellationRegistry()
        worker = MinecraftWorker(
            store=store,
            bridge=bridge,
            cancellations=cancellations,
        )
        service = MinecraftTaskService(
            store=store,
            bridge=bridge,
            cancellations=cancellations,
            worker=worker,
        )
        task = await service.create_task(
            goal=ResourceGoal(resource="oak_log", quantity=4),
            user_id="u",
            session_id="s",
            bot_id="test-bot",
        )
        running = asyncio.create_task(service.run_to_completion(task.task_id))
        await bridge.action_started.wait()
        await service.cancel_task(task.task_id, user_id="u", session_id="s")
        assert store.get(task.task_id).cancel_requested
        bridge.release_action.set()
        result = await running
        return result, bridge

    result, bridge = asyncio.run(scenario())
    assert result.task.status is TaskStatus.CANCELLED
    assert len(bridge.actions) == 1
