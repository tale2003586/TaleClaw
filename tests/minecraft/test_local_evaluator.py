from applications.minecraft.local_evaluator import LocalEvaluator
from applications.minecraft.models import (
    ActionEvent,
    ActionStatus,
    BotObservation,
    InventoryItem,
    LocalEvaluationType,
    MinecraftTask,
    PlanStep,
    Position,
    ResourceGoal,
)


def observation(count):
    return BotObservation(
        position=Position(x=0, y=64, z=0),
        inventory=(InventoryItem(item="oak_log", count=count),),
    )


def task():
    return MinecraftTask(
        user_id="u",
        session_id="s",
        bot_id="b",
        goal=ResourceGoal(resource="oak_log", quantity=4),
    )


def step():
    return PlanStep(
        step_id="collect",
        goal="collect wood",
        action="collect_blocks",
        arguments={"resource": "oak_log", "count": 4},
    )


def test_real_inventory_change_completes_task():
    result = LocalEvaluator().evaluate(
        step=step(),
        before=observation(0),
        result=ActionEvent(action_id="a", status=ActionStatus.SUCCEEDED),
        after=observation(4),
        task=task(),
        retry_count=0,
        retry_limit=2,
    )
    assert result.result is LocalEvaluationType.TASK_COMPLETED


def test_known_failure_is_local_then_escalates():
    evaluator = LocalEvaluator()
    event = ActionEvent(
        action_id="a",
        status=ActionStatus.FAILED,
        error_code="path_failed",
    )
    local = evaluator.evaluate(
        step=step(),
        before=observation(0),
        result=event,
        after=observation(0),
        task=task(),
        retry_count=0,
        retry_limit=1,
    )
    escalated = evaluator.evaluate(
        step=step(),
        before=observation(0),
        result=event,
        after=observation(0),
        task=task(),
        retry_count=1,
        retry_limit=1,
    )
    assert local.result is LocalEvaluationType.LOCAL_RECOVERY
    assert escalated.result is LocalEvaluationType.ESCALATE
