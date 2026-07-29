"""Provider-aware prompt budgeting and the final model-call guard."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any, Iterable
import copy

from runtime.token_estimator import (
    context_window_tokens,
    estimate_tokens,
    output_reserve_tokens,
)


DEFAULT_SOFT_TRIGGER_RATIO = 0.70
DEFAULT_COMPACTION_TARGET_RATIO = 0.45
DEFAULT_HARD_INPUT_RATIO = 0.92
DEFAULT_SAFETY_MARGIN_RATIO = 0.03


class PromptBudgetExceeded(RuntimeError):
    """Raised before provider invocation when the prompt cannot fit safely."""

    def __init__(self, *, actual_tokens: int, hard_limit_tokens: int, budget: "DynamicPromptBudget") -> None:
        self.actual_tokens = max(0, int(actual_tokens))
        self.hard_limit_tokens = max(0, int(hard_limit_tokens))
        self.budget = budget
        super().__init__(
            "model call blocked: prompt uses "
            f"{self.actual_tokens} tokens, hard limit is {self.hard_limit_tokens}"
        )


@dataclass(frozen=True)
class DynamicPromptBudget:
    model_context_window: int
    system_prompt_tokens: int
    tool_definition_tokens: int
    reserved_output_tokens: int
    safety_margin_tokens: int
    usable_input_tokens: int
    soft_compaction_trigger: int
    compaction_target: int
    hard_input_limit: int
    hard_prompt_limit: int
    soft_trigger_ratio: float = DEFAULT_SOFT_TRIGGER_RATIO
    compaction_target_ratio: float = DEFAULT_COMPACTION_TARGET_RATIO
    hard_input_ratio: float = DEFAULT_HARD_INPUT_RATIO

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PromptTokenUsage:
    actual_prompt_tokens: int
    dynamic_content_tokens: int
    hard_prompt_limit: int
    hard_dynamic_limit: int
    over_hard_limit: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_dynamic_prompt_budget(
    *,
    provider: Any | None = None,
    system_messages: Iterable[dict[str, Any]] = (),
    tools: Iterable[dict[str, Any]] = (),
    reserved_output_tokens: int = 0,
    safety_margin_tokens: int | None = None,
    soft_trigger_ratio: float = DEFAULT_SOFT_TRIGGER_RATIO,
    compaction_target_ratio: float = DEFAULT_COMPACTION_TARGET_RATIO,
    hard_input_ratio: float = DEFAULT_HARD_INPUT_RATIO,
) -> DynamicPromptBudget:
    """Calculate budgets from the selected provider's actual context window.

    ``usable_input_tokens`` deliberately excludes the fixed system/tool cost. The
    final guard compares the complete prompt with ``hard_prompt_limit`` so those
    fixed costs can never disappear from accounting.
    """

    window = max(1, int(context_window_tokens(provider)))
    system = estimate_tokens(list(system_messages), provider=provider)
    tool_tokens = _tool_definition_tokens(tools, provider=provider)
    output = output_reserve_tokens(
        provider,
        requested_output_tokens=max(0, int(reserved_output_tokens or 0)),
        context_window=window,
    )
    margin = (
        max(0, int(safety_margin_tokens))
        if safety_margin_tokens is not None
        else max(1, int(window * DEFAULT_SAFETY_MARGIN_RATIO))
    )
    margin = min(margin, max(0, window - output - 1))
    usable = max(1, window - system - tool_tokens - output - margin)
    soft_ratio = _bounded_ratio(soft_trigger_ratio, DEFAULT_SOFT_TRIGGER_RATIO)
    target_ratio = _bounded_ratio(compaction_target_ratio, DEFAULT_COMPACTION_TARGET_RATIO)
    hard_ratio = _bounded_ratio(hard_input_ratio, DEFAULT_HARD_INPUT_RATIO)
    hard_dynamic = max(1, int(usable * hard_ratio))
    hard_prompt = min(
        max(1, window - tool_tokens - output - margin),
        system + hard_dynamic,
    )
    return DynamicPromptBudget(
        model_context_window=window,
        system_prompt_tokens=system,
        tool_definition_tokens=tool_tokens,
        reserved_output_tokens=output,
        safety_margin_tokens=margin,
        usable_input_tokens=usable,
        soft_compaction_trigger=max(1, int(usable * soft_ratio)),
        compaction_target=max(1, int(usable * target_ratio)),
        hard_input_limit=hard_dynamic,
        hard_prompt_limit=hard_prompt,
        soft_trigger_ratio=soft_ratio,
        compaction_target_ratio=target_ratio,
        hard_input_ratio=hard_ratio,
    )


def measure_prompt(
    messages: list[dict[str, Any]],
    *,
    budget: DynamicPromptBudget,
    provider: Any | None = None,
) -> PromptTokenUsage:
    actual = estimate_tokens(messages, provider=provider)
    dynamic = max(0, actual - budget.system_prompt_tokens)
    return PromptTokenUsage(
        actual_prompt_tokens=actual,
        dynamic_content_tokens=dynamic,
        hard_prompt_limit=budget.hard_prompt_limit,
        hard_dynamic_limit=budget.hard_input_limit,
        over_hard_limit=(
            actual > budget.hard_prompt_limit
            or dynamic > budget.hard_input_limit
        ),
    )


def enforce_hard_token_guard(
    messages: list[dict[str, Any]],
    *,
    budget: DynamicPromptBudget,
    provider: Any | None = None,
) -> PromptTokenUsage:
    usage = measure_prompt(messages, budget=budget, provider=provider)
    if usage.over_hard_limit:
        raise PromptBudgetExceeded(
            actual_tokens=usage.actual_prompt_tokens,
            hard_limit_tokens=usage.hard_prompt_limit,
            budget=budget,
        )
    return usage


def select_complete_groups(
    groups: list[list[dict[str, Any]]],
    *,
    max_tokens: int,
    provider: Any | None = None,
    required_group_indexes: Iterable[int] = (),
) -> list[dict[str, Any]]:
    """Select a newest-first tail without splitting message/tool groups."""

    if not groups or max_tokens <= 0:
        return []
    required = {index for index in required_group_indexes if 0 <= index < len(groups)}
    selected = set(required)
    for index in range(len(groups) - 1, -1, -1):
        candidate = selected | {index}
        rendered = _flatten_selected(groups, candidate)
        if estimate_tokens(rendered, provider=provider) <= max_tokens:
            selected = candidate
    return _flatten_selected(groups, selected)


def reduce_prompt_to_hard_limit(
    messages: list[dict[str, Any]],
    *,
    max_tokens: int,
    provider: Any | None = None,
) -> list[dict[str, Any]]:
    """Drop low-priority context and whole old groups without truncating text."""
    items = [copy.deepcopy(item) for item in messages if isinstance(item, dict)]
    if estimate_tokens(items, provider=provider) <= max_tokens:
        return items
    system = [item for item in items if str(item.get("role") or "") == "system"]
    non_system = [item for item in items if str(item.get("role") or "") != "system"]
    state_messages = [item for item in non_system if _message_kind(item) in {
        "task_state_context", "coding_context_state",
    }]
    evidence_messages = [item for item in non_system if _message_kind(item) == "retrieved_evidence_context"]
    conversation = [
        item for item in non_system
        if item not in state_messages and item not in evidence_messages
    ]
    groups = _message_groups(conversation)
    required: set[int] = set()
    latest_instruction = _latest_instruction_group(groups)
    if latest_instruction is not None:
        required.add(latest_instruction)
    required.update(index for index, group in enumerate(groups) if not _group_is_closed(group))

    fixed = [*system, *state_messages]
    fixed_tokens = estimate_tokens(fixed, provider=provider)
    selected = select_complete_groups(
        groups,
        max_tokens=max(0, max_tokens - fixed_tokens),
        provider=provider,
        required_group_indexes=required,
    )
    candidate = [*fixed, *selected]
    # Retrieved evidence is lowest recency priority and is included only whole.
    for evidence in evidence_messages:
        proposed = [*candidate, evidence]
        if estimate_tokens(proposed, provider=provider) <= max_tokens:
            candidate = proposed
    return candidate


def _tool_definition_tokens(tools: Iterable[dict[str, Any]], *, provider: Any | None) -> int:
    payload = list(tools or [])
    if not payload:
        return 0
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return estimate_tokens(
        [{"role": "system", "content": serialized}],
        provider=provider,
    )


def _flatten_selected(groups: list[list[dict[str, Any]]], indexes: set[int]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        if index in indexes:
            result.extend(dict(message) for message in group if isinstance(message, dict))
    return result


def _bounded_ratio(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(1.0, max(0.01, parsed))


def _message_kind(message: dict[str, Any]) -> str:
    metadata = message.get("metadata")
    return str(metadata.get("kind") or "") if isinstance(metadata, dict) else ""


def _message_groups(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages:
        if str(message.get("role") or "") == "user" and current:
            groups.append(current)
            current = [message]
        else:
            current.append(message)
    if current:
        groups.append(current)
    return groups


def _latest_instruction_group(groups: list[list[dict[str, Any]]]) -> int | None:
    for index in range(len(groups) - 1, -1, -1):
        for message in groups[index]:
            if str(message.get("role") or "") != "user":
                continue
            if _message_kind(message) not in {"retrieved_evidence_context", "task_state_context"}:
                return index
    return None


def _group_is_closed(group: list[dict[str, Any]]) -> bool:
    expected: set[str] = set()
    seen: set[str] = set()
    for message in group:
        if str(message.get("role") or "") == "assistant":
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id"):
                    expected.add(str(call["id"]))
        elif str(message.get("role") or "") == "tool":
            seen.add(str(message.get("tool_call_id") or ""))
    return not (expected - seen)
