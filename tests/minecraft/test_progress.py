from applications.minecraft.models import MinecraftTask, ResourceGoal, TaskStatus
from applications.minecraft.reporting import build_terminal_report
from applications.minecraft.state_machine import (
    goal_completed,
    net_acquired,
    transition,
    with_observation_count,
)


def _task(quantity=30, baseline=5, current=5):
    return MinecraftTask(
        user_id="u",
        session_id="s",
        bot_id="b",
        goal=ResourceGoal(resource="diamond", quantity=quantity),
        status=TaskStatus.OBSERVING,
        baseline_count=baseline,
        current_count=current,
    )


def test_baseline_inventory_truth_and_loss():
    task = _task()
    task = with_observation_count(task, 34)
    assert task.net_acquired == 29
    assert not goal_completed(task)
    task = with_observation_count(task, 35)
    assert task.status is TaskStatus.SUCCEEDED
    assert task.net_acquired == 30
    assert net_acquired(baseline_count=5, current_count=4) == 0


def test_terminal_reports_have_required_fields():
    for status in (
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
        TaskStatus.CANCELLED,
    ):
        task = _task(quantity=1, baseline=0, current=1)
        terminal = transition(task, status, error_code=status.value)
        report = build_terminal_report(terminal, inventory_summary={"diamond": 1})
        assert report.status == status.value
        assert report.resource == "diamond"
        assert report.reason_code
