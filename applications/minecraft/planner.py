from __future__ import annotations

from .catalog import DomainCatalog
from .context import MinecraftPlannerContextBuilder
from .model_gateway import MinecraftModelError, MinecraftModelGateway
from .models import (
    BridgeActionType,
    CognitiveDecision,
    PlanSource,
    PlanStep,
    TaskPlan,
    MinecraftTask,
)
from .plan_validator import PlanValidator
from .world_state import BeliefWorldState


class MinecraftPlanner:
    def __init__(
        self,
        *,
        gateway: MinecraftModelGateway,
        context_builder: MinecraftPlannerContextBuilder,
        validator: PlanValidator,
        catalog: DomainCatalog,
        max_revisions: int = 1,
    ) -> None:
        self.gateway = gateway
        self.context_builder = context_builder
        self.validator = validator
        self.catalog = catalog
        self.max_revisions = max(0, int(max_revisions))

    def create_plan(
        self,
        *,
        task: MinecraftTask,
        world: BeliefWorldState,
        approval: CognitiveDecision,
        recent_events: tuple[dict, ...] = (),
    ) -> TaskPlan:
        messages = self.context_builder.build(
            task=task,
            world=world,
            catalog=self.catalog,
            purpose="initial_plan" if task.plan_version == 0 else "global_replan",
            recent_events=recent_events,
        )
        last_errors = ()
        for attempt in range(self.max_revisions + 1):
            current_messages = list(messages)
            if last_errors:
                current_messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Revise the plan to fix these validator errors: "
                            + ", ".join(last_errors)
                        ),
                    }
                )
            try:
                result = self.gateway.run_structured(
                    purpose="planner" if attempt == 0 else "plan_revision",
                    messages=current_messages,
                    schema=TaskPlan,
                    approval=approval,
                )
            except MinecraftModelError:
                break
            plan = result.value
            errors = self.validator.validate(plan, task=task, world=world)
            if not errors:
                return plan
            last_errors = errors
        return fallback_plan(
            task,
            event_id=approval.event_id,
            version=max(task.plan_version + 1, 1),
            world=world,
        )


def fallback_plan(
    task: MinecraftTask,
    *,
    event_id: str,
    version: int,
    world: BeliefWorldState | None = None,
) -> TaskPlan:
    resource = task.goal.resource
    inventory = _inventory(world)
    if resource == "stone_pickaxe":
        raw_steps = (
            ("logs", "Collect wood", BridgeActionType.COLLECT_BLOCKS, {"resource": "oak_log", "count": 3}),
            ("planks", "Craft planks", BridgeActionType.CRAFT, {"item": "oak_planks", "count": 8}),
            ("sticks", "Craft sticks", BridgeActionType.CRAFT, {"item": "stick", "count": 2}),
            ("wood_pick", "Craft wooden pickaxe", BridgeActionType.CRAFT, {"item": "wooden_pickaxe", "count": 1}),
            ("cobble", "Collect cobblestone", BridgeActionType.COLLECT_BLOCKS, {"resource": "cobblestone", "count": 3}),
            ("stone_pick", "Craft stone pickaxe", BridgeActionType.CRAFT, {"item": "stone_pickaxe", "count": 1}),
        )
    elif resource == "diamond":
        if any(inventory.get(item, 0) for item in (
            "iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe"
        )):
            remaining = max(1, task.goal.quantity - task.net_acquired)
            raw_steps = (
                (
                    "diamonds",
                    "Mine the next bounded diamond batch",
                    BridgeActionType.COLLECT_BLOCKS,
                    {"resource": "diamond", "count": min(8, remaining)},
                ),
            )
        else:
            raw_steps = (
                ("logs", "Collect wood", BridgeActionType.COLLECT_BLOCKS, {"resource": "oak_log", "count": 3}),
                ("planks", "Craft planks", BridgeActionType.CRAFT, {"item": "oak_planks", "count": 12}),
                ("sticks", "Craft sticks", BridgeActionType.CRAFT, {"item": "stick", "count": 8}),
                ("wood_pick", "Craft wooden pickaxe", BridgeActionType.CRAFT, {"item": "wooden_pickaxe", "count": 1}),
                ("cobble", "Collect furnace and pickaxe stone", BridgeActionType.COLLECT_BLOCKS, {"resource": "cobblestone", "count": 11}),
                ("stone_pick", "Craft stone pickaxe", BridgeActionType.CRAFT, {"item": "stone_pickaxe", "count": 1}),
                ("coal", "Collect furnace fuel", BridgeActionType.COLLECT_BLOCKS, {"resource": "coal", "count": 2}),
                ("iron", "Collect iron ore", BridgeActionType.COLLECT_BLOCKS, {"resource": "raw_iron", "count": 3}),
                ("furnace", "Craft furnace", BridgeActionType.CRAFT, {"item": "furnace", "count": 1}),
                ("smelt_iron", "Smelt iron ingots", BridgeActionType.SMELT, {"input": "raw_iron", "fuel": "coal", "output": "iron_ingot", "count": 3}),
                ("iron_pick", "Craft iron pickaxe", BridgeActionType.CRAFT, {"item": "iron_pickaxe", "count": 1}),
                ("diamonds", "Mine the first bounded diamond batch", BridgeActionType.COLLECT_BLOCKS, {"resource": "diamond", "count": min(8, task.goal.quantity)}),
            )
    else:
        raw_steps = (
            ("find", f"Find {resource}", BridgeActionType.FIND_BLOCKS, {"resource": resource, "count": task.goal.quantity}),
            ("collect", f"Collect {resource}", BridgeActionType.COLLECT_BLOCKS, {"resource": resource, "count": task.goal.quantity}),
        )
    return TaskPlan(
        plan_version=version,
        source=PlanSource.FALLBACK,
        situation="Model plan unavailable or invalid; use bounded safe fallback.",
        strategy=f"bounded_{resource}_fallback",
        steps=tuple(
            PlanStep(
                step_id=step_id,
                goal=goal,
                action=action,
                arguments=arguments,
                success_conditions=(f"step:{step_id}:observed",),
            )
            for step_id, goal, action, arguments in raw_steps
        ),
        replan_when=("path_failed", "safety_interrupt", "plan_exhausted"),
        triggering_event_id=event_id,
    )


def _inventory(world: BeliefWorldState | None) -> dict[str, int]:
    if world is None:
        return {}
    fact = world.facts.get("inventory")
    if fact is None or not isinstance(fact.value, dict):
        return {}
    return {str(key): int(value) for key, value in fact.value.items()}
