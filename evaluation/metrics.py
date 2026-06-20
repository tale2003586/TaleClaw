from __future__ import annotations

from collections import Counter
from statistics import mean

from runtime.trace.failure import FailureCategory


def summarize_rows(rows: list[dict]) -> dict:
    total = len(rows)
    passed = sum(1 for row in rows if row.get("passed"))
    within_budget = sum(1 for row in rows if row.get("within_budget"))
    verifier_passes = sum(1 for row in rows if row.get("verifier_passed"))
    diff_passes = sum(1 for row in rows if row.get("workspace_diff_passed"))
    trace_passes = sum(1 for row in rows if row.get("trace_passed"))
    failure_counts = Counter(
        row.get("failure_category", "unknown")
        for row in rows
        if not row.get("passed")
    )
    return {
        "total_tasks": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": _rate(passed, total),
        "within_budget": within_budget,
        "within_budget_rate": _rate(within_budget, total),
        "verifier_passes": verifier_passes,
        "verifier_pass_rate": _rate(verifier_passes, total),
        "workspace_diff_pass_rate": _rate(diff_passes, total),
        "trace_completeness_rate": _rate(trace_passes, total),
        "avg_reasoning_steps": _avg(row.get("reasoning_steps", 0) for row in rows),
        "avg_tool_calls": _avg(row.get("tool_calls", 0) for row in rows),
        "failure_category_counts": dict(sorted(failure_counts.items())),
    }


def failure_category(row: dict) -> str:
    if row.get("run_status") != "completed":
        return FailureCategory.RUN_FAILED.value
    if not row.get("within_budget"):
        return FailureCategory.BUDGET_EXCEEDED.value
    if not row.get("verifier_passed"):
        return FailureCategory.VERIFIER_ERROR.value
    if not row.get("workspace_diff_passed"):
        return FailureCategory.WORKSPACE_DIFF_FAILED.value
    if not row.get("trace_passed"):
        return FailureCategory.TRACE_MISSING.value
    return FailureCategory.UNKNOWN.value


def _rate(value: int, total: int) -> float:
    return value / total if total else 0.0


def _avg(values) -> float:
    data = [float(value or 0) for value in values]
    return mean(data) if data else 0.0
