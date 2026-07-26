import json

import pytest
from models.model_task_runner import ModelTaskResult
from runtime.context.budget import ContextBudgeter

from applications.minecraft.catalog import CATALOG
from applications.minecraft.context import MinecraftCriticContextBuilder
from applications.minecraft.llm_critic import LLMCritic
from applications.minecraft.model_gateway import MinecraftModelGateway
from applications.minecraft.models import (
    CognitiveDecision,
    CognitiveDecisionType,
    MinecraftTask,
    ResourceGoal,
)
from applications.minecraft.planner import fallback_plan
from applications.minecraft.world_state import BeliefWorldState


class Runner:
    def __init__(self):
        self.calls = 0

    def run_result(self, **_kwargs):
        self.calls += 1
        return ModelTaskResult(
            content=json.dumps(
                {
                    "diagnosis": "path is repeatedly blocked",
                    "excluded_strategy": "same_path",
                    "recommendation": "select another resource cluster",
                }
            )
        )


def _task():
    return MinecraftTask(
        user_id="u",
        session_id="s",
        bot_id="b",
        goal=ResourceGoal(resource="oak_log", quantity=4),
    )


def test_llm_critic_requires_escalation_approval():
    runner = Runner()
    critic = LLMCritic(
        gateway=MinecraftModelGateway(runner=runner),
        context_builder=MinecraftCriticContextBuilder(
            budgeter=ContextBudgeter(enabled=False)
        ),
        catalog=CATALOG,
    )
    task = _task()
    plan = fallback_plan(task, event_id="e", version=1)
    denied = CognitiveDecision(
        decision=CognitiveDecisionType.EXECUTE_NEXT_STEP,
        reason_code="normal",
        event_id="e",
        plan_version=1,
    )
    with pytest.raises(PermissionError):
        critic.critique(
            task=task,
            world=BeliefWorldState(task_id=task.task_id),
            plan=plan,
            failures=(),
            approval=denied,
        )
    assert runner.calls == 0

    approved = denied.model_copy(
        update={
            "decision": CognitiveDecisionType.CALL_LLM_CRITIC,
            "consumes_model_budget": True,
        }
    )
    result = critic.critique(
        task=task,
        world=BeliefWorldState(task_id=task.task_id),
        plan=plan,
        failures=({"error": "path_failed"},),
        approval=approved,
    )
    assert result.excluded_strategy == "same_path"
    assert runner.calls == 1
