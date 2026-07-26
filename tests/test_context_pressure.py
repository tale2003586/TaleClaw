from copy import deepcopy
from types import SimpleNamespace

import pytest

from runtime.context.pressure import (
    ContextCategoryUsage,
    ContextPressureLevel,
    evaluate_context_pressure,
)
from runtime.context import ContextBuilder
from runtime.sessions import Session


@pytest.mark.parametrize(("total", "expected"), [
    (100, ContextPressureLevel.LOW),
    (600, ContextPressureLevel.MEDIUM),
    (800, ContextPressureLevel.HIGH),
    (950, ContextPressureLevel.CRITICAL),
])
def test_usage_ratio_boundaries(total, expected) -> None:
    result = evaluate_context_pressure(total_tokens=total, context_window=1000)
    assert result.level is expected


@pytest.mark.parametrize(("categories", "reason"), [
    (ContextCategoryUsage(tool_output_tokens=700), "tool_output_dominant"),
    (ContextCategoryUsage(memory_tokens=350, retrieval_tokens=350), "memory_retrieval_dominant"),
    (ContextCategoryUsage(code_context_tokens=700), "code_context_dominant"),
])
def test_category_imbalance_can_raise_pressure(categories, reason) -> None:
    result = evaluate_context_pressure(
        total_tokens=1000,
        context_window=10000,
        categories=categories,
    )
    assert result.level is ContextPressureLevel.CRITICAL
    assert reason in result.policy_hint.reason_codes


def test_large_tool_results_and_long_task_raise_pressure() -> None:
    result = evaluate_context_pressure(
        total_tokens=600,
        context_window=1000,
        large_tool_result_count=2,
        long_running_task=True,
    )
    assert result.level is ContextPressureLevel.HIGH
    assert result.policy_hint.consider_tool_result_compaction is True


def test_invalid_counts_are_sanitized_and_zero_window_is_safe() -> None:
    result = evaluate_context_pressure(
        total_tokens=-3,
        context_window=0,
        categories=ContextCategoryUsage(memory_tokens=-1),
        candidate_memory_count=-2,
    )
    assert result.total_tokens == 0
    assert result.usage_ratio == 0.0
    assert result.categories.memory_tokens == 0


def test_evaluator_does_not_mutate_input_and_is_deterministic() -> None:
    categories = ContextCategoryUsage(tool_output_tokens=500, recent_turn_tokens=100)
    before = deepcopy(categories)
    first = evaluate_context_pressure(
        total_tokens=1000,
        context_window=2000,
        categories=categories,
    )
    second = evaluate_context_pressure(
        total_tokens=1000,
        context_window=2000,
        categories=categories,
    )
    assert categories == before
    assert first == second
    assert first.to_dict()["level"] == "high"


def test_observation_adds_report_metadata_without_changing_prompt() -> None:
    session = Session(id="pressure:observation")
    session.add_message("user", "keep this prompt identical")
    profile = SimpleNamespace(system_prompt="system", tool_mode="bot")

    disabled = ContextBuilder(pressure_observation_enabled=False).build(
        session=session,
        profile=profile,
    )
    enabled = ContextBuilder(pressure_observation_enabled=True).build(
        session=session,
        profile=profile,
    )

    assert enabled.messages == disabled.messages
    assert "context_pressure" not in disabled.report.metadata
    assert enabled.report.metadata["context_pressure"]["level"] == "low"
