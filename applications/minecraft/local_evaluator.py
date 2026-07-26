from __future__ import annotations

from .models import (
    ActionEvent,
    ActionStatus,
    BotObservation,
    LocalEvaluation,
    LocalEvaluationType,
    MinecraftTask,
    PlanStep,
)


class LocalEvaluator:
    def evaluate(
        self,
        *,
        step: PlanStep,
        before: BotObservation,
        result: ActionEvent,
        after: BotObservation,
        task: MinecraftTask,
        retry_count: int,
        retry_limit: int,
        no_progress_seconds: float = 0,
    ) -> LocalEvaluation:
        before_count = before.item_count(task.goal.resource)
        after_count = after.item_count(task.goal.resource)
        delta = after_count - before_count
        if max(0, after_count - task.baseline_count) >= task.goal.quantity:
            return LocalEvaluation(
                result=LocalEvaluationType.TASK_COMPLETED,
                reason_code="inventory_goal_reached",
                progress_delta=delta,
            )
        if result.status is ActionStatus.SUCCEEDED:
            if _success_observed(step, before, after) or delta > 0:
                return LocalEvaluation(
                    result=LocalEvaluationType.STEP_SUCCEEDED,
                    reason_code="expected_change_observed",
                    progress_delta=delta,
                )
            return LocalEvaluation(
                result=LocalEvaluationType.ESCALATE,
                reason_code="success_without_expected_change",
                progress_delta=delta,
            )
        if result.status is ActionStatus.CANCELLED:
            return LocalEvaluation(
                result=LocalEvaluationType.BLOCKED,
                reason_code="action_cancelled",
            )
        known = result.error_code in {
            "path_failed",
            "resource_not_found",
            "tool_broken",
            "inventory_full",
        }
        if known and retry_count < retry_limit:
            return LocalEvaluation(
                result=LocalEvaluationType.LOCAL_RECOVERY,
                reason_code=str(result.error_code),
                retryable=True,
            )
        if no_progress_seconds > 120 or known:
            return LocalEvaluation(
                result=LocalEvaluationType.ESCALATE,
                reason_code="recovery_exhausted" if known else "no_progress",
            )
        return LocalEvaluation(
            result=LocalEvaluationType.FAILED,
            reason_code=str(result.error_code or "unknown_action_failure"),
        )


def _success_observed(
    step: PlanStep,
    before: BotObservation,
    after: BotObservation,
) -> bool:
    if step.action.value == "find_blocks":
        return bool(after.nearby_blocks)
    if step.action.value in {"collect_blocks", "craft", "smelt"}:
        return before.inventory != after.inventory
    if step.action.value == "equip":
        return before.equipment != after.equipment
    return True
