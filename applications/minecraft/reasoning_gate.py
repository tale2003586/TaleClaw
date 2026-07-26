from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from time import monotonic

from .models import (
    CognitiveDecision,
    CognitiveDecisionType,
    LocalEvaluation,
    LocalEvaluationType,
    MinecraftTask,
    TaskPlan,
)


@dataclass
class ReasoningGateState:
    remaining_model_calls: int
    processed_event_ids: set[str] = field(default_factory=set)
    failure_counts: Counter = field(default_factory=Counter)
    excluded_strategies: set[str] = field(default_factory=set)
    last_model_call_at: float | None = None


class ReasoningGate:
    def __init__(
        self,
        *,
        model_call_budget: int,
        cooldown_seconds: float = 1,
        failure_threshold: int = 2,
        clock=monotonic,
    ) -> None:
        self.state = ReasoningGateState(max(0, int(model_call_budget)))
        self.cooldown_seconds = max(0, float(cooldown_seconds))
        self.failure_threshold = max(1, int(failure_threshold))
        self.clock = clock

    def fork(self, *, model_call_budget: int | None = None) -> "ReasoningGate":
        """Create isolated decision state for one long-running task."""
        return ReasoningGate(
            model_call_budget=(
                self.state.remaining_model_calls
                if model_call_budget is None
                else model_call_budget
            ),
            cooldown_seconds=self.cooldown_seconds,
            failure_threshold=self.failure_threshold,
            clock=self.clock,
        )

    def decide(
        self,
        *,
        event_id: str,
        event_type: str,
        task: MinecraftTask,
        plan: TaskPlan | None,
        evaluation: LocalEvaluation | None = None,
        current_plan_version: int | None = None,
        strategy: str = "",
        safety_event: bool = False,
        user_cancelled: bool = False,
    ) -> CognitiveDecision:
        plan_version = current_plan_version if current_plan_version is not None else task.plan_version
        if event_id in self.state.processed_event_ids:
            return self._decision(
                CognitiveDecisionType.CONTINUE_PLAN,
                "duplicate_event",
                event_id,
                plan_version,
            )
        self.state.processed_event_ids.add(event_id)

        if user_cancelled:
            return self._decision(
                CognitiveDecisionType.BLOCK_TASK,
                "user_cancelled",
                event_id,
                plan_version,
                cancel=True,
            )
        if safety_event:
            return self._decision(
                CognitiveDecisionType.SAFETY_INTERRUPT,
                "safety_event",
                event_id,
                plan_version,
                cancel=True,
            )
        if event_type in {"tick", "action_progress", "ordinary_discovery"}:
            return self._decision(
                CognitiveDecisionType.CONTINUE_PLAN,
                "non_cognitive_event",
                event_id,
                plan_version,
            )
        if evaluation and evaluation.result is LocalEvaluationType.TASK_COMPLETED:
            return self._decision(
                CognitiveDecisionType.COMPLETE_TASK,
                evaluation.reason_code,
                event_id,
                plan_version,
            )
        if plan is not None and plan.plan_version != plan_version:
            return self._decision(
                CognitiveDecisionType.CONTINUE_PLAN,
                "stale_plan_event",
                event_id,
                plan_version,
            )
        if evaluation and evaluation.result is LocalEvaluationType.LOCAL_RECOVERY:
            key = evaluation.reason_code
            self.state.failure_counts[key] += 1
            if self.state.failure_counts[key] < self.failure_threshold:
                return self._decision(
                    CognitiveDecisionType.LOCAL_RECOVERY,
                    key,
                    event_id,
                    plan_version,
                )
            return self._model_decision(
                CognitiveDecisionType.CALL_LLM_CRITIC,
                "local_recovery_exhausted",
                event_id,
                plan_version,
            )
        if evaluation and evaluation.result in {
            LocalEvaluationType.BLOCKED,
            LocalEvaluationType.FAILED,
        }:
            decision = (
                CognitiveDecisionType.BLOCK_TASK
                if evaluation.result is LocalEvaluationType.BLOCKED
                else CognitiveDecisionType.FAIL_TASK
            )
            return self._decision(
                decision,
                evaluation.reason_code,
                event_id,
                plan_version,
            )

        model_reason = None
        decision_type = CognitiveDecisionType.CALL_PLANNER
        if plan is None:
            model_reason = "new_task_without_plan"
        elif event_type == "plan_exhausted":
            model_reason = "rolling_plan_exhausted"
        elif (
            evaluation
            and evaluation.result is LocalEvaluationType.ESCALATE
        ):
            model_reason = evaluation.reason_code
            decision_type = CognitiveDecisionType.CALL_LLM_CRITIC

        if model_reason:
            if strategy:
                self.state.excluded_strategies.add(strategy)
            return self._model_decision(
                decision_type,
                model_reason,
                event_id,
                plan_version,
            )
        if evaluation and evaluation.result is LocalEvaluationType.STEP_SUCCEEDED:
            return self._decision(
                CognitiveDecisionType.EXECUTE_NEXT_STEP,
                "step_succeeded",
                event_id,
                plan_version,
            )
        return self._decision(
            CognitiveDecisionType.CONTINUE_PLAN,
            "plan_still_valid",
            event_id,
            plan_version,
        )

    def _model_decision(self, decision, reason, event_id, plan_version):
        if self.state.remaining_model_calls <= 0:
            return self._decision(
                CognitiveDecisionType.BLOCK_TASK,
                "model_budget_exhausted",
                event_id,
                plan_version,
            )
        now = self.clock()
        if (
            self.state.last_model_call_at is not None
            and now - self.state.last_model_call_at < self.cooldown_seconds
        ):
            return self._decision(
                CognitiveDecisionType.CONTINUE_PLAN,
                "model_cooldown",
                event_id,
                plan_version,
            )
        self.state.remaining_model_calls -= 1
        self.state.last_model_call_at = now
        return self._decision(
            decision,
            reason,
            event_id,
            plan_version,
            consumes=True,
        )

    @staticmethod
    def _decision(
        decision,
        reason,
        event_id,
        plan_version,
        *,
        consumes=False,
        cancel=False,
    ):
        return CognitiveDecision(
            decision=decision,
            reason_code=reason,
            event_id=event_id,
            plan_version=plan_version,
            consumes_model_budget=consumes,
            cancel_current_action=cancel,
            suggested_next_step_type=decision.value,
        )
