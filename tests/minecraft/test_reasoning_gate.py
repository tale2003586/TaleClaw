from applications.minecraft.models import (
    LocalEvaluation,
    LocalEvaluationType,
    MinecraftTask,
    ResourceGoal,
)
from applications.minecraft.reasoning_gate import ReasoningGate


def task():
    return MinecraftTask(
        user_id="u",
        session_id="s",
        bot_id="b",
        goal=ResourceGoal(resource="oak_log", quantity=4),
    )


def test_new_task_calls_planner_once_and_duplicate_is_ignored():
    gate = ReasoningGate(model_call_budget=2, cooldown_seconds=0)
    first = gate.decide(
        event_id="e1",
        event_type="new_task",
        task=task(),
        plan=None,
    )
    duplicate = gate.decide(
        event_id="e1",
        event_type="new_task",
        task=task(),
        plan=None,
    )
    assert first.decision.value == "call_planner"
    assert first.consumes_model_budget
    assert duplicate.reason_code == "duplicate_event"
    assert gate.state.remaining_model_calls == 1


def test_tick_and_safety_never_call_model():
    gate = ReasoningGate(model_call_budget=2)
    tick = gate.decide(event_id="tick", event_type="tick", task=task(), plan=None)
    safety = gate.decide(
        event_id="lava",
        event_type="hazard",
        task=task(),
        plan=None,
        safety_event=True,
    )
    assert tick.decision.value == "continue_plan"
    assert safety.decision.value == "safety_interrupt"
    assert gate.state.remaining_model_calls == 2


def test_recovery_threshold_escalates_once_and_budget_exhausts():
    gate = ReasoningGate(model_call_budget=1, cooldown_seconds=0, failure_threshold=2)
    local = LocalEvaluation(
        result=LocalEvaluationType.LOCAL_RECOVERY,
        reason_code="path_failed",
        retryable=True,
    )
    one = gate.decide(
        event_id="f1",
        event_type="step_result",
        task=task(),
        plan=None,
        evaluation=local,
    )
    two = gate.decide(
        event_id="f2",
        event_type="step_result",
        task=task(),
        plan=None,
        evaluation=local,
    )
    three = gate.decide(
        event_id="f3",
        event_type="step_result",
        task=task(),
        plan=None,
        evaluation=local,
    )
    assert one.decision.value == "local_recovery"
    assert two.decision.value == "call_llm_critic"
    assert three.reason_code == "model_budget_exhausted"
