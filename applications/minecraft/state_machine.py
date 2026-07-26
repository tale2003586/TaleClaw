from __future__ import annotations

from dataclasses import replace

from .models import MinecraftTask, TaskStatus, now_iso


_ALLOWED = {
    TaskStatus.PENDING: {TaskStatus.CONNECTING, TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.CONNECTING: {
        TaskStatus.OBSERVING,
        TaskStatus.RECOVERING,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
    },
    TaskStatus.OBSERVING: {
        TaskStatus.PLANNING,
        TaskStatus.EXECUTING,
        TaskStatus.SUCCEEDED,
        TaskStatus.RECOVERING,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
    },
    TaskStatus.PLANNING: {
        TaskStatus.EXECUTING,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
    },
    TaskStatus.EXECUTING: {
        TaskStatus.OBSERVING,
        TaskStatus.PLANNING,
        TaskStatus.RECOVERING,
        TaskStatus.SUCCEEDED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
    },
    TaskStatus.RECOVERING: {
        TaskStatus.OBSERVING,
        TaskStatus.EXECUTING,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
    },
}


def net_acquired(*, baseline_count: int, current_count: int) -> int:
    return max(0, int(current_count) - int(baseline_count))


def goal_completed(task: MinecraftTask) -> bool:
    return net_acquired(
        baseline_count=task.baseline_count,
        current_count=task.current_count,
    ) >= task.goal.quantity


def transition(
    task: MinecraftTask,
    status: TaskStatus,
    *,
    error_code: str | None = None,
    error_message: str = "",
) -> MinecraftTask:
    if task.status == status:
        return task
    if task.status.terminal:
        raise ValueError(f"terminal task cannot transition from {task.status}")
    if status not in _ALLOWED.get(task.status, set()):
        raise ValueError(f"illegal task transition: {task.status} -> {status}")
    finished_at = now_iso() if status.terminal else task.finished_at
    started_at = task.started_at
    if started_at is None and status not in {TaskStatus.PENDING, TaskStatus.CONNECTING}:
        started_at = now_iso()
    return task.model_copy(
        update={
            "status": status,
            "version": task.version + 1,
            "updated_at": now_iso(),
            "started_at": started_at,
            "finished_at": finished_at,
            "error_code": error_code,
            "error_message": error_message,
        }
    )


def with_observation_count(task: MinecraftTask, current_count: int) -> MinecraftTask:
    updated = task.model_copy(
        update={
            "current_count": max(0, int(current_count)),
            "version": task.version + 1,
            "updated_at": now_iso(),
        }
    )
    if goal_completed(updated) and not updated.status.terminal:
        return transition(updated, TaskStatus.SUCCEEDED)
    return updated
