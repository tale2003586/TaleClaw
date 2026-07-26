from __future__ import annotations

from datetime import datetime

from .models import MinecraftTask, TerminalReport


def build_terminal_report(
    task: MinecraftTask,
    *,
    inventory_summary: dict[str, int] | None = None,
    stages: tuple[str, ...] = (),
) -> TerminalReport:
    if not task.status.terminal:
        raise ValueError("terminal report requires a terminal task")
    started = _parse(task.started_at or task.created_at)
    finished = _parse(task.finished_at or task.updated_at)
    next_step = ""
    if task.status.value == "blocked":
        next_step = "检查服务器环境、资源预算或机器人状态后重新开始。"
    elif task.status.value == "failed":
        next_step = "查看错误分类和任务 trace 后重试。"
    return TerminalReport(
        task_id=task.task_id,
        status=task.status.value,
        resource=task.goal.resource,
        requested_quantity=task.goal.quantity,
        net_acquired=task.net_acquired,
        elapsed_seconds=max(0, (finished - started).total_seconds()),
        reason_code=task.error_code or task.status.value,
        stage_summary=stages,
        inventory_summary=dict(inventory_summary or {}),
        next_step=next_step,
    )


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)
