import json

from models.model_task_runner import ModelTaskResult
from runtime.context.budget import ContextBudgeter

from applications.minecraft.catalog import CATALOG
from applications.minecraft.context import MinecraftPlannerContextBuilder
from applications.minecraft.model_gateway import MinecraftModelError, MinecraftModelGateway
from applications.minecraft.models import (
    CognitiveDecision,
    CognitiveDecisionType,
    MinecraftTask,
    PlanSource,
    ResourceGoal,
    TaskPlan,
)
from applications.minecraft.plan_validator import PlanValidator
from applications.minecraft.planner import MinecraftPlanner
from applications.minecraft.world_state import BeliefWorldState


class Runner:
    def __init__(self, content):
        self.content = content
        self.calls = 0

    def run_result(self, **_kwargs):
        self.calls += 1
        return ModelTaskResult(content=self.content, elapsed_ms=3)


def approval(kind=CognitiveDecisionType.CALL_PLANNER):
    return CognitiveDecision(
        decision=kind,
        reason_code="new_task_without_plan",
        event_id="event-1",
        plan_version=0,
        consumes_model_budget=True,
    )


def task(resource="oak_log"):
    return MinecraftTask(
        user_id="u",
        session_id="s",
        bot_id="b",
        goal=ResourceGoal(resource=resource, quantity=1),
    )


def test_model_gateway_requires_reasoning_gate_approval():
    gateway = MinecraftModelGateway(runner=Runner("{}"))
    denied = approval(CognitiveDecisionType.CONTINUE_PLAN).model_copy(
        update={"consumes_model_budget": False}
    )
    try:
        gateway.run_structured(
            purpose="planner",
            messages=[],
            schema=TaskPlan,
            approval=denied,
        )
    except MinecraftModelError as exc:
        assert exc.code == "reasoning_gate_approval_required"
    else:
        raise AssertionError("gateway accepted an unapproved model call")


def test_planner_accepts_valid_plan_and_falls_back_on_invalid_output():
    payload = {
        "plan_version": 1,
        "source": "model",
        "situation": "wood nearby",
        "strategy": "collect nearby wood",
        "steps": [
            {
                "step_id": "collect",
                "goal": "collect wood",
                "action": "collect_blocks",
                "arguments": {"resource": "oak_log", "count": 1},
                "success_conditions": ["inventory oak_log increases"],
            }
        ],
        "replan_when": ["path_failed"],
        "triggering_event_id": "event-1",
    }
    world = BeliefWorldState(task_id="task")
    planner = _planner(Runner(json.dumps(payload)))
    plan = planner.create_plan(task=task(), world=world, approval=approval())
    assert plan.source is PlanSource.MODEL

    fallback = _planner(Runner("not-json")).create_plan(
        task=task("stone_pickaxe"),
        world=world,
        approval=approval(),
    )
    assert fallback.source is PlanSource.FALLBACK
    assert fallback.steps[-1].arguments["item"] == "stone_pickaxe"


def _planner(runner):
    return MinecraftPlanner(
        gateway=MinecraftModelGateway(runner=runner),
        context_builder=MinecraftPlannerContextBuilder(
            budgeter=ContextBudgeter(enabled=False)
        ),
        validator=PlanValidator(catalog=CATALOG),
        catalog=CATALOG,
    )
