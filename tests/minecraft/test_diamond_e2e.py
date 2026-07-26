import asyncio

from applications.minecraft.catalog import CATALOG
from applications.minecraft.local_evaluator import LocalEvaluator
from applications.minecraft.models import ResourceGoal
from applications.minecraft.planner import fallback_plan
from applications.minecraft.reasoning_gate import ReasoningGate
from applications.minecraft.service import MinecraftTaskService
from applications.minecraft.stores.memory import InMemoryMinecraftTaskStore
from applications.minecraft.worker import MinecraftWorker
from runtime.cancellation import CancellationRegistry

from .fakes import FakeBridge


class FallbackPlanner:
    def create_plan(self, *, task, world, approval):
        return fallback_plan(
            task,
            event_id=approval.event_id,
            version=max(1, task.plan_version + 1),
            world=world,
        )


def test_empty_inventory_collects_net_new_thirty_diamonds():
    bridge = FakeBridge(
        inventory={},
        available={
            "oak_log": 8,
            "cobblestone": 32,
            "coal": 16,
            "raw_iron": 16,
            "diamond": 30,
        },
    )
    store = InMemoryMinecraftTaskStore()
    cancellations = CancellationRegistry()
    worker = MinecraftWorker(
        store=store,
        bridge=bridge,
        cancellations=cancellations,
        planner=FallbackPlanner(),
        evaluator=LocalEvaluator(),
        reasoning_gate=ReasoningGate(model_call_budget=2, cooldown_seconds=0),
        catalog=CATALOG,
    )
    service = MinecraftTaskService(
        store=store,
        bridge=bridge,
        cancellations=cancellations,
        worker=worker,
    )

    async def scenario():
        task = await service.create_task(
            goal=ResourceGoal(resource="diamond", quantity=30),
            user_id="u",
            session_id="s",
            bot_id="test-bot",
        )
        return await service.run_to_completion(task.task_id)

    result = asyncio.run(scenario())
    assert result.task.status.value == "succeeded"
    assert result.task.net_acquired == 30
    assert store.latest_checkpoint(result.task.task_id) is not None
    assert len({action.idempotency_key for action in bridge.actions}) == len(bridge.actions)
