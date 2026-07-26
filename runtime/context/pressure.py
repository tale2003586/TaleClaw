from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from math import isfinite


class ContextPressureLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class ContextCategoryUsage:
    system_tokens: int = 0
    developer_tokens: int = 0
    recent_turn_tokens: int = 0
    tool_output_tokens: int = 0
    memory_tokens: int = 0
    retrieval_tokens: int = 0
    code_context_tokens: int = 0

    def normalized(self) -> "ContextCategoryUsage":
        return ContextCategoryUsage(**{
            key: _non_negative_int(value)
            for key, value in asdict(self).items()
        })


@dataclass(frozen=True)
class ContextPressurePolicyHint:
    preserve_core_instructions: bool = True
    consider_tool_result_compaction: bool = False
    consider_retrieval_reduction: bool = False
    consider_code_context_compaction: bool = False
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["reason_codes"] = list(self.reason_codes)
        return data


@dataclass(frozen=True)
class ContextPressureSnapshot:
    total_tokens: int
    context_window: int
    usage_ratio: float
    level: ContextPressureLevel
    categories: ContextCategoryUsage = field(default_factory=ContextCategoryUsage)
    candidate_memory_count: int = 0
    retrieved_note_count: int = 0
    large_tool_result_count: int = 0
    long_running_task: bool = False
    policy_hint: ContextPressurePolicyHint = field(
        default_factory=ContextPressurePolicyHint
    )

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["level"] = self.level.value
        data["policy_hint"] = self.policy_hint.to_dict()
        return data


@dataclass(frozen=True)
class ContextPressureThresholds:
    medium_ratio: float = 0.55
    high_ratio: float = 0.75
    critical_ratio: float = 0.92
    category_high_ratio: float = 0.45
    category_critical_ratio: float = 0.65
    large_tool_result_high: int = 2
    large_tool_result_critical: int = 5


def evaluate_context_pressure(
    *,
    total_tokens: int,
    context_window: int,
    categories: ContextCategoryUsage | None = None,
    candidate_memory_count: int = 0,
    retrieved_note_count: int = 0,
    large_tool_result_count: int = 0,
    long_running_task: bool = False,
    thresholds: ContextPressureThresholds | None = None,
) -> ContextPressureSnapshot:
    """Pure, deterministic pressure observation. It never changes context."""
    limits = thresholds or ContextPressureThresholds()
    total = _non_negative_int(total_tokens)
    window = _non_negative_int(context_window)
    usage = min(1.0, total / window) if window else (1.0 if total else 0.0)
    usage = usage if isfinite(usage) else 1.0
    normalized = (categories or ContextCategoryUsage()).normalized()
    candidate_count = _non_negative_int(candidate_memory_count)
    retrieved_count = _non_negative_int(retrieved_note_count)
    large_count = _non_negative_int(large_tool_result_count)

    reasons: list[str] = []
    score = 0
    if usage >= limits.critical_ratio:
        score = 3
        reasons.append("token_usage_critical")
    elif usage >= limits.high_ratio:
        score = 2
        reasons.append("token_usage_high")
    elif usage >= limits.medium_ratio:
        score = 1
        reasons.append("token_usage_medium")

    denominator = max(total, 1)
    category_values = {
        "tool_output": normalized.tool_output_tokens,
        "memory_retrieval": normalized.memory_tokens + normalized.retrieval_tokens,
        "code_context": normalized.code_context_tokens,
    }
    for name, value in category_values.items():
        ratio = value / denominator
        if ratio >= limits.category_critical_ratio and value > 0:
            score = max(score, 3)
            reasons.append(f"{name}_dominant")
        elif ratio >= limits.category_high_ratio and value > 0:
            score = max(score, 2)
            reasons.append(f"{name}_high")

    if large_count >= limits.large_tool_result_critical:
        score = max(score, 3)
        reasons.append("many_large_tool_results")
    elif large_count >= limits.large_tool_result_high:
        score = max(score, 2)
        reasons.append("large_tool_results")
    if long_running_task and (usage >= limits.medium_ratio or large_count > 0):
        score = max(score, 2)
        reasons.append("long_running_task")
    if candidate_count + retrieved_count >= 20 and usage >= limits.medium_ratio:
        score = max(score, 2)
        reasons.append("memory_candidate_volume")

    level = (
        ContextPressureLevel.LOW,
        ContextPressureLevel.MEDIUM,
        ContextPressureLevel.HIGH,
        ContextPressureLevel.CRITICAL,
    )[min(score, 3)]
    hint = ContextPressurePolicyHint(
        consider_tool_result_compaction=(
            normalized.tool_output_tokens / denominator >= limits.category_high_ratio
            or large_count >= limits.large_tool_result_high
        ),
        consider_retrieval_reduction=(
            (normalized.memory_tokens + normalized.retrieval_tokens) / denominator
            >= limits.category_high_ratio
        ),
        consider_code_context_compaction=(
            normalized.code_context_tokens / denominator >= limits.category_high_ratio
        ),
        reason_codes=tuple(dict.fromkeys(reasons)),
    )
    return ContextPressureSnapshot(
        total_tokens=total,
        context_window=window,
        usage_ratio=round(usage, 6),
        level=level,
        categories=normalized,
        candidate_memory_count=candidate_count,
        retrieved_note_count=retrieved_count,
        large_tool_result_count=large_count,
        long_running_task=bool(long_running_task),
        policy_hint=hint,
    )


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0
