from datetime import datetime, timezone

from applications.minecraft.catalog import CATALOG
from applications.minecraft.models import (
    BotObservation,
    InventoryItem,
    MinecraftTask,
    PlanSource,
    PlanStep,
    Position,
    ResourceGoal,
    TaskPlan,
)
from applications.minecraft.plan_validator import PlanValidator
from applications.minecraft.world_state import BeliefWorldState


def _task():
    return MinecraftTask(
        user_id="u",
        session_id="s",
        bot_id="b",
        goal=ResourceGoal(resource="diamond", quantity=1),
    )


def _plan(preconditions=()):
    return TaskPlan(
        plan_version=1,
        source=PlanSource.MODEL,
        strategy="mine diamond",
        steps=(
            PlanStep(
                step_id="diamond",
                goal="collect diamond",
                action="collect_blocks",
                arguments={"resource": "diamond", "count": 1},
                preconditions=preconditions,
            ),
        ),
        triggering_event_id="e1",
    )


def test_diamond_requires_iron_pickaxe():
    validator = PlanValidator(catalog=CATALOG)
    empty = BeliefWorldState(task_id="task")
    assert "insufficient_tool:diamond" in validator.validate(
        _plan(), task=_task(), world=empty
    )
    observation = BotObservation(
        observed_at=datetime.now(timezone.utc).isoformat(),
        position=Position(x=0, y=64, z=0),
        inventory=(InventoryItem(item="iron_pickaxe", count=1),),
    )
    equipped = empty.merge(observation)
    assert validator.validate(_plan(), task=_task(), world=equipped) == ()


def test_unknown_dependency_is_rejected():
    errors = PlanValidator(catalog=CATALOG).validate(
        _plan(("step:missing",)),
        task=_task(),
        world=BeliefWorldState(task_id="task"),
    )
    assert "unknown_dependency:missing" in errors


def test_plan_may_build_iron_pickaxe_before_diamond_step():
    plan = TaskPlan(
        plan_version=1,
        source=PlanSource.MODEL,
        strategy="build tool then mine",
        steps=(
            PlanStep(
                step_id="pick",
                goal="craft iron pickaxe",
                action="craft",
                arguments={"item": "iron_pickaxe", "count": 1},
            ),
            PlanStep(
                step_id="diamond",
                goal="collect diamond",
                action="collect_blocks",
                arguments={"resource": "diamond", "count": 1},
            ),
        ),
        triggering_event_id="e1",
    )
    assert PlanValidator(catalog=CATALOG).validate(
        plan, task=_task(), world=BeliefWorldState(task_id="task")
    ) == ()
