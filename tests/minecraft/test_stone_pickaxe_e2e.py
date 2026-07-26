import asyncio

from models.model_task_runner import ModelTaskResult
from runtime.cancellation import CancellationRegistry
from runtime.context.budget import ContextBudgeter

from applications.minecraft.catalog import CATALOG
from applications.minecraft.context import MinecraftPlannerContextBuilder
from applications.minecraft.local_evaluator import LocalEvaluator
from applications.minecraft.model_gateway import MinecraftModelGateway
from applications.minecraft.models import MinecraftTask, ResourceGoal, TaskStatus, now_iso
from applications.minecraft.plan_validator import PlanValidator
from applications.minecraft.planner import MinecraftPlanner
from applications.minecraft.reasoning_gate import ReasoningGate
from applications.minecraft.stores.memory import InMemoryMinecraftTaskStore
from applications.minecraft.worker import MinecraftWorker
from tests.minecraft.fakes import FakeBridge


class InvalidRunner:
    def __init__(self):
        self.calls = 0

    def run_result(self, **_kwargs):
        self.calls += 1
        return ModelTaskResult(content="invalid")


def test_stone_pickaxe_fallback_plan_end_to_end():
    asyncio.run(_stone_pickaxe_fallback_plan_end_to_end())


async def _stone_pickaxe_fallback_plan_end_to_end():
    runner = InvalidRunner()
    planner = MinecraftPlanner(
        gateway=MinecraftModelGateway(runner=runner),
        context_builder=MinecraftPlannerContextBuilder(
            budgeter=ContextBudgeter(enabled=False)
        ),
        validator=PlanValidator(catalog=CATALOG),
        catalog=CATALOG,
        max_revisions=0,
    )
    store = InMemoryMinecraftTaskStore()
    bridge = FakeBridge(available={"oak_log": 8, "cobblestone": 8})
    await bridge.connect()
    task = store.create(
        MinecraftTask(
            user_id="u",
            session_id="s",
            bot_id="test-bot",
            goal=ResourceGoal(resource="stone_pickaxe", quantity=1),
            status=TaskStatus.OBSERVING,
            started_at=now_iso(),
            cancellation_scope="minecraft:stone",
        ),
        idempotency_key="stone-pickaxe",
    )
    worker = MinecraftWorker(
        store=store,
        bridge=bridge,
        cancellations=CancellationRegistry(),
        planner=planner,
        evaluator=LocalEvaluator(),
        reasoning_gate=ReasoningGate(model_call_budget=2, cooldown_seconds=0),
        catalog=CATALOG,
    )
    result = await worker.run(task.task_id)
    assert result.task.status is TaskStatus.SUCCEEDED
    assert bridge.inventory["stone_pickaxe"] >= 1
    assert runner.calls == 1
