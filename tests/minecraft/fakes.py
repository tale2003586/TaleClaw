from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from applications.minecraft.models import (
    ActionEvent,
    ActionHandle,
    ActionStatus,
    BotObservation,
    BridgeAction,
    BridgeActionType,
    InventoryItem,
    NearbyBlock,
    Position,
)
from runtime.cancellation import CancellationToken


class FakeBridge:
    def __init__(
        self,
        *,
        inventory: dict[str, int] | None = None,
        available: dict[str, int] | None = None,
    ) -> None:
        self.inventory = dict(inventory or {})
        self.available = dict(available or {"oak_log": 64})
        self.actions: list[BridgeAction] = []
        self._handles: dict[str, tuple[BridgeAction, ActionStatus]] = {}
        self._by_key: dict[str, str] = {}
        self.cancelled: set[str] = set()
        self.connected = False

    async def connect(self) -> BotObservation:
        self.connected = True
        return self._observation()

    async def observe(self) -> BotObservation:
        return self._observation()

    async def submit_action(self, action: BridgeAction) -> ActionHandle:
        existing = self._by_key.get(action.idempotency_key)
        if existing:
            return ActionHandle(action_id=existing, status=self._handles[existing][1])
        action_id = f"action-{len(self.actions) + 1}"
        self.actions.append(action)
        self._by_key[action.idempotency_key] = action_id
        self._handles[action_id] = (action, ActionStatus.PENDING)
        return ActionHandle(action_id=action_id, status=ActionStatus.PENDING)

    async def watch_action(
        self,
        action_id: str,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ActionEvent]:
        action, _ = self._handles[action_id]
        self._handles[action_id] = (action, ActionStatus.RUNNING)
        yield ActionEvent(action_id=action_id, status=ActionStatus.RUNNING, progress=0)
        await asyncio.sleep(0)
        if cancellation_token.requested() or action_id in self.cancelled:
            self._handles[action_id] = (action, ActionStatus.CANCELLED)
            yield ActionEvent(action_id=action_id, status=ActionStatus.CANCELLED)
            return
        if action.type is BridgeActionType.FIND_BLOCKS:
            resource = str(action.arguments["resource"])
            status = (
                ActionStatus.SUCCEEDED
                if self.available.get(resource, 0) > 0
                else ActionStatus.FAILED
            )
            self._handles[action_id] = (action, status)
            yield ActionEvent(
                action_id=action_id,
                status=status,
                error_code=None if status is ActionStatus.SUCCEEDED else "resource_not_found",
            )
            return
        if action.type is BridgeActionType.COLLECT_BLOCKS:
            resource = str(action.arguments["resource"])
            wanted = int(action.arguments["count"])
            collected = min(wanted, self.available.get(resource, 0))
            if collected:
                self.available[resource] -= collected
                self.inventory[resource] = self.inventory.get(resource, 0) + collected
                status = ActionStatus.SUCCEEDED
                error = None
            else:
                status = ActionStatus.FAILED
                error = "resource_not_found"
            self._handles[action_id] = (action, status)
            yield ActionEvent(
                action_id=action_id,
                status=status,
                error_code=error,
                observation=self._observation(),
            )
            return
        if action.type is BridgeActionType.CRAFT:
            item = str(action.arguments["item"])
            count = int(action.arguments.get("count", 1))
            if not self._craft(item, count):
                self._handles[action_id] = (action, ActionStatus.FAILED)
                yield ActionEvent(
                    action_id=action_id,
                    status=ActionStatus.FAILED,
                    error_code="missing_ingredients",
                )
                return
            self._handles[action_id] = (action, ActionStatus.SUCCEEDED)
            yield ActionEvent(
                action_id=action_id,
                status=ActionStatus.SUCCEEDED,
                observation=self._observation(),
            )
            return
        if action.type is BridgeActionType.SMELT:
            count = int(action.arguments.get("count", 1))
            input_item = str(action.arguments.get("input", ""))
            fuel = str(action.arguments.get("fuel", "coal"))
            output = str(action.arguments.get("output", ""))
            fuel_count = max(1, (count + 7) // 8)
            if (
                self.inventory.get(input_item, 0) < count
                or self.inventory.get(fuel, 0) < fuel_count
                or self.inventory.get("furnace", 0) < 1
            ):
                self._handles[action_id] = (action, ActionStatus.FAILED)
                yield ActionEvent(
                    action_id=action_id,
                    status=ActionStatus.FAILED,
                    error_code="missing_smelting_materials",
                )
                return
            self.inventory[input_item] -= count
            self.inventory[fuel] -= fuel_count
            self.inventory[output] = self.inventory.get(output, 0) + count
            self._handles[action_id] = (action, ActionStatus.SUCCEEDED)
            yield ActionEvent(
                action_id=action_id,
                status=ActionStatus.SUCCEEDED,
                observation=self._observation(),
            )
            return
        self._handles[action_id] = (action, ActionStatus.SUCCEEDED)
        yield ActionEvent(action_id=action_id, status=ActionStatus.SUCCEEDED)

    async def cancel_action(self, action_id: str) -> None:
        self.cancelled.add(action_id)

    async def disconnect(self) -> None:
        self.connected = False

    def _craft(self, item: str, count: int) -> bool:
        recipes = {
            "oak_planks": ({"oak_log": 1}, 4),
            "stick": ({"oak_planks": 2}, 4),
            "wooden_pickaxe": ({"oak_planks": 3, "stick": 2}, 1),
            "stone_pickaxe": ({"cobblestone": 3, "stick": 2}, 1),
            "iron_pickaxe": ({"iron_ingot": 3, "stick": 2}, 1),
            "furnace": ({"cobblestone": 8}, 1),
        }
        recipe = recipes.get(item)
        if recipe is None:
            return False
        ingredients, produced = recipe
        crafts = max(1, (count + produced - 1) // produced)
        if any(self.inventory.get(name, 0) < needed * crafts for name, needed in ingredients.items()):
            return False
        for name, needed in ingredients.items():
            self.inventory[name] -= needed * crafts
        self.inventory[item] = self.inventory.get(item, 0) + produced * crafts
        return True

    def _observation(self) -> BotObservation:
        blocks = []
        for resource, count in self.available.items():
            for index in range(min(count, 8)):
                blocks.append(
                    NearbyBlock(
                        block=resource,
                        position=Position(x=index + 1, y=64, z=0),
                        distance=index + 1,
                    )
                )
        return BotObservation(
            connected=self.connected,
            bot_id="test-bot",
            server_id="test-server",
            world_id="test-world",
            version="1.21.1",
            position=Position(x=0, y=64, z=0),
            inventory=tuple(
                InventoryItem(item=item, count=count)
                for item, count in sorted(self.inventory.items())
            ),
            nearby_blocks=tuple(blocks[:128]),
        )
