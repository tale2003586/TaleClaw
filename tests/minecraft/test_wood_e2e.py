import asyncio

from applications.minecraft.reporting import build_terminal_report
from applications.minecraft.service import MinecraftTaskService
from applications.minecraft.stores.memory import InMemoryMinecraftTaskStore
from runtime.cancellation import CancellationRegistry
from tests.minecraft.fakes import FakeBridge


def test_collect_four_logs_end_to_end():
    asyncio.run(_collect_four_logs_end_to_end())


async def _collect_four_logs_end_to_end():
    bridge = FakeBridge(available={"oak_log": 8})
    store = InMemoryMinecraftTaskStore()
    cancellations = CancellationRegistry()
    service = MinecraftTaskService(
        store=store,
        bridge=bridge,
        cancellations=cancellations,
    )
    task = await service.create_task(
        text="/minecraft 收集 4 个原木",
        user_id="user",
        session_id="cli:test",
        bot_id="test-bot",
    )
    result = await service.run_to_completion(task.task_id)
    assert result.task.status.value == "succeeded"
    assert result.task.net_acquired == 4
    assert len(bridge.actions) == 2
    report = build_terminal_report(
        result.task,
        inventory_summary=bridge.inventory,
        stages=result.stages,
    )
    assert report.net_acquired == 4
    await service.close()


def test_terminal_task_does_not_submit_more_actions():
    asyncio.run(_terminal_task_does_not_submit_more_actions())


async def _terminal_task_does_not_submit_more_actions():
    bridge = FakeBridge(inventory={"oak_log": 4}, available={"oak_log": 8})
    service = MinecraftTaskService(
        store=InMemoryMinecraftTaskStore(),
        bridge=bridge,
        cancellations=CancellationRegistry(),
    )
    task = await service.create_task(
        text="收集 4 个原木",
        user_id="user",
        session_id="cli:test",
        bot_id="test-bot",
    )
    bridge.inventory["oak_log"] = 8
    result = await service.run_to_completion(task.task_id)
    assert result.task.status.value == "succeeded"
    assert bridge.actions == []
