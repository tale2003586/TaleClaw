from __future__ import annotations

from .catalog import DomainCatalog
from .models import BridgeAction, BridgeActionType, MinecraftTask, TaskPlan
from .world_state import BeliefWorldState


class PlanValidator:
    def __init__(
        self,
        *,
        catalog: DomainCatalog,
        allowed_actions: set[BridgeActionType] | None = None,
    ) -> None:
        self.catalog = catalog
        self.allowed_actions = allowed_actions or set(BridgeActionType)

    def validate(
        self,
        plan: TaskPlan,
        *,
        task: MinecraftTask,
        world: BeliefWorldState,
    ) -> tuple[str, ...]:
        errors: list[str] = []
        iron_pickaxe_available = _has_iron_pickaxe(world)
        ids = [step.step_id for step in plan.steps]
        if len(ids) != len(set(ids)):
            errors.append("duplicate_step_id")
        edges: dict[str, set[str]] = {step_id: set() for step_id in ids}
        for step in plan.steps:
            if step.action not in self.allowed_actions:
                errors.append(f"action_not_allowed:{step.step_id}")
            try:
                BridgeAction(
                    type=step.action,
                    arguments=step.arguments,
                    idempotency_key=f"validate-{step.step_id}",
                )
            except Exception:
                errors.append(f"invalid_arguments:{step.step_id}")
            for condition in step.preconditions:
                if condition.startswith("step:"):
                    dependency = condition.split(":", 1)[1]
                    if dependency not in edges:
                        errors.append(f"unknown_dependency:{dependency}")
                    else:
                        edges[step.step_id].add(dependency)
            if step.action is BridgeActionType.COLLECT_BLOCKS:
                resource_id = str(step.arguments.get("resource") or "")
                try:
                    resource = self.catalog.resource(resource_id)
                except ValueError:
                    errors.append(f"unknown_resource:{step.step_id}")
                    continue
                if resource.minimum_tool_tier >= 3 and not iron_pickaxe_available:
                    errors.append(f"insufficient_tool:{step.step_id}")
            if (
                step.action is BridgeActionType.CRAFT
                and str(step.arguments.get("item") or "") in {
                    "iron_pickaxe",
                    "diamond_pickaxe",
                    "netherite_pickaxe",
                }
            ):
                iron_pickaxe_available = True
            count = int(step.arguments.get("count") or 0)
            if count > task.budget.mined_blocks:
                errors.append(f"mining_budget_exceeded:{step.step_id}")
        if _has_cycle(edges):
            errors.append("plan_dependency_cycle")
        return tuple(dict.fromkeys(errors))


def _has_iron_pickaxe(world: BeliefWorldState) -> bool:
    inventory = world.facts.get("inventory")
    if inventory is None or not isinstance(inventory.value, dict):
        return False
    return any(
        int(inventory.value.get(item, 0)) > 0
        for item in ("iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe")
    )


def _has_cycle(edges: dict[str, set[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for dependency in edges[node]:
            if visit(dependency):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in edges)
