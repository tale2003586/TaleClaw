import asyncio

from applications.minecraft.models import BridgeAction, BridgeActionType
from runtime.cancellation import CancellationRegistry
from tests.minecraft.fakes import FakeBridge


def test_fake_bridge_collects_deterministically_and_deduplicates():
    asyncio.run(_collects_deterministically_and_deduplicates())


async def _collects_deterministically_and_deduplicates():
    bridge = FakeBridge(available={"oak_log": 4})
    await bridge.connect()
    action = BridgeAction(
        type=BridgeActionType.COLLECT_BLOCKS,
        arguments={"resource": "oak_log", "count": 4},
        idempotency_key="collect-0001",
    )
    first = await bridge.submit_action(action)
    second = await bridge.submit_action(action)
    assert first.action_id == second.action_id
    token = CancellationRegistry().register("test")
    events = [event async for event in bridge.watch_action(first.action_id, token)]
    assert events[-1].status.value == "succeeded"
    assert (await bridge.observe()).item_count("oak_log") == 4
    assert len(bridge.actions) == 1
