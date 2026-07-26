from __future__ import annotations

import json

from runtime.context.budget import ContextBudgeter

from .catalog import DomainCatalog
from .memory_adapter import MinecraftMemoryAdapter
from .models import MinecraftTask, TaskPlan
from .world_state import BeliefWorldState


class MinecraftPlannerContextBuilder:
    def __init__(
        self,
        *,
        budgeter: ContextBudgeter,
        memory: MinecraftMemoryAdapter | None = None,
        max_chars: int = 12000,
    ) -> None:
        self.budgeter = budgeter
        self.memory = memory
        self.max_chars = max(1000, int(max_chars))

    def build(
        self,
        *,
        task: MinecraftTask,
        world: BeliefWorldState,
        catalog: DomainCatalog,
        purpose: str = "initial_plan",
        recent_events: tuple[dict, ...] = (),
    ) -> list[dict]:
        memory = (
            self.memory.retrieve(
                f"{task.goal.resource} {task.goal.quantity}",
                purpose=purpose,
                budget_chars=2000,
            )
            if self.memory
            else ""
        )
        resource = catalog.resource(task.goal.resource)
        sections = {
            "goal": task.goal.model_dump(),
            "world": world.context_summary(max_chars=5000),
            "catalog": {
                "resource_id": resource.resource_id,
                "blocks": resource.blocks,
                "drop_item": resource.drop_item,
                "minimum_tool_tier": resource.minimum_tool_tier,
                "prerequisites": resource.prerequisites,
                "recipes": [
                    {
                        "output": recipe.output,
                        "output_count": recipe.output_count,
                        "ingredients": dict(recipe.ingredients),
                        "station": recipe.station,
                    }
                    for recipe in catalog.recipes()
                ],
                "allowed_actions": (
                    "observe",
                    "find_blocks",
                    "collect_blocks",
                    "craft",
                    "smelt",
                    "equip",
                    "eat",
                    "return_safe",
                    "branch_mine",
                ),
            },
            "budget": task.budget.model_dump(),
            "recent_events": recent_events[-20:],
            "memory": memory,
        }
        rendered = self._render(sections)
        return [
            {
                "role": "system",
                "content": (
                    "You are the Minecraft resource planner. Return strict JSON only. "
                    "Use only the supplied high-level actions and never emit code, shell, "
                    "raw packets, commands, attacks, containers, explosives, or lava actions."
                ),
            },
            {"role": "user", "content": rendered},
        ]

    def _render(self, sections: dict) -> str:
        blocks = []
        for name, value in sections.items():
            raw = json.dumps(value, ensure_ascii=False, default=str)
            budget_name = "memory" if name == "memory" else "active_turn"
            rendered = self.budgeter.apply(budget_name, raw).rendered_text
            blocks.append(f"<{name}>{rendered}</{name}>")
        return "\n".join(blocks)[: self.max_chars]


class MinecraftCriticContextBuilder(MinecraftPlannerContextBuilder):
    def build_critic(
        self,
        *,
        task: MinecraftTask,
        world: BeliefWorldState,
        catalog: DomainCatalog,
        plan: TaskPlan,
        failures: tuple[dict, ...],
    ) -> list[dict]:
        messages = self.build(
            task=task,
            world=world,
            catalog=catalog,
            purpose="llm_critic",
            recent_events=failures,
        )
        messages[0]["content"] = (
            "You are a Minecraft strategy critic. Return strict JSON only with "
            "diagnosis, excluded_strategy, and recommendation. Do not emit actions."
        )
        messages[-1]["content"] += (
            "\n<current_plan>"
            + plan.model_dump_json()
            + "</current_plan>"
        )
        return messages
