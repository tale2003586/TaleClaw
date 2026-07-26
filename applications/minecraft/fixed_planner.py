from __future__ import annotations

from .models import BridgeAction, BridgeActionType, MinecraftTask


def wood_collection_actions(task: MinecraftTask, *, cycle: int = 0) -> tuple[BridgeAction, ...]:
    remaining = max(0, task.goal.quantity - task.net_acquired)
    if remaining == 0:
        return ()
    prefix = f"{task.task_id}:{cycle}"
    return (
        BridgeAction(
            type=BridgeActionType.FIND_BLOCKS,
            arguments={
                "resource": task.goal.resource,
                "count": remaining,
                "max_distance": task.budget.search_distance,
            },
            idempotency_key=f"{prefix}:find:{remaining}",
        ),
        BridgeAction(
            type=BridgeActionType.COLLECT_BLOCKS,
            arguments={
                "resource": task.goal.resource,
                "count": remaining,
                "max_distance": task.budget.search_distance,
            },
            idempotency_key=f"{prefix}:collect:{remaining}",
            timeout_seconds=120,
        ),
    )
