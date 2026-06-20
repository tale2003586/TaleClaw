from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any


DEFAULT_TOTAL_BUDGET_CHARS = 24000


@dataclass(frozen=True)
class SectionBudgetRule:
    name: str
    budget_chars: int
    floor_chars: int
    strategy: str
    keep_head_turns: int = 0
    keep_tail_turns: int = 0
    summary_chars: int = 0
    keep_recent_results: int = 5
    preserve_tools: tuple[str, ...] = (
        "read_file",
        "list_files",
        "git_diff",
        "git_status",
        "git_log",
        "code_outline",
        "repo_map",
    )


@dataclass(frozen=True)
class BudgetedText:
    name: str
    raw_text: str
    rendered_text: str
    budget_chars: int | None = None
    floor_chars: int = 0
    strategy: str = "none"
    truncated: bool = False
    reduction: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ContextBudgeter:
    def __init__(
        self,
        *,
        enabled: bool = False,
        total_budget_chars: int | None = None,
        rules: dict[str, SectionBudgetRule] | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.total_budget_chars = total_budget_chars
        self.rules = dict(rules or {})

    @classmethod
    def from_env(cls) -> "ContextBudgeter":
        enabled = _env_bool("CONTEXT_ENABLE_SECTION_BUDGET", default=True)
        total_budget = _env_int("CONTEXT_BUDGET_CHARS", DEFAULT_TOTAL_BUDGET_CHARS)
        return cls(
            enabled=enabled,
            total_budget_chars=total_budget,
            rules={
                "mode_instructions": SectionBudgetRule(
                    name="mode_instructions",
                    budget_chars=_env_int("CONTEXT_MODE_INSTRUCTIONS_BUDGET", 3000),
                    floor_chars=_env_int("CONTEXT_MODE_INSTRUCTIONS_FLOOR", 1000),
                    strategy="head",
                ),
                "project_instructions": SectionBudgetRule(
                    name="project_instructions",
                    budget_chars=_env_int("CONTEXT_PROJECT_INSTRUCTIONS_BUDGET", 3000),
                    floor_chars=_env_int("CONTEXT_PROJECT_INSTRUCTIONS_FLOOR", 1000),
                    strategy="head",
                ),
                "memory": SectionBudgetRule(
                    name="memory",
                    budget_chars=_env_int("CONTEXT_MEMORY_BUDGET", 2000),
                    floor_chars=_env_int("CONTEXT_MEMORY_FLOOR", 500),
                    strategy="head_tail",
                ),
                "working_memory": SectionBudgetRule(
                    name="working_memory",
                    budget_chars=_env_int("WORKING_MEMORY_CONTEXT_BUDGET", 4000),
                    floor_chars=_env_int("WORKING_MEMORY_CONTEXT_FLOOR", 1000),
                    strategy="head_tail",
                ),
                "retrieved_history": SectionBudgetRule(
                    name="retrieved_history",
                    budget_chars=_env_int_any(
                        ["CONTEXT_RETRIEVED_HISTORY_BUDGET", "CONTEXT_RETRIEVED_MEMORY_BUDGET"],
                        2500,
                    ),
                    floor_chars=_env_int_any(
                        ["CONTEXT_RETRIEVED_HISTORY_FLOOR", "CONTEXT_RETRIEVED_MEMORY_FLOOR"],
                        500,
                    ),
                    strategy="head_tail",
                ),
                "security_knowledge": SectionBudgetRule(
                    name="security_knowledge",
                    budget_chars=_env_int("CONTEXT_SECURITY_KNOWLEDGE_BUDGET", 3000),
                    floor_chars=_env_int("CONTEXT_SECURITY_KNOWLEDGE_FLOOR", 800),
                    strategy="head_tail",
                ),
                "task_runtime_events": SectionBudgetRule(
                    name="task_runtime_events",
                    budget_chars=_env_int("CONTEXT_TASK_RUNTIME_EVENTS_BUDGET", 1500),
                    floor_chars=_env_int("CONTEXT_TASK_RUNTIME_EVENTS_FLOOR", 300),
                    strategy="tail",
                ),
                "conversation_history": SectionBudgetRule(
                    name="conversation_history",
                    budget_chars=_env_int("CONTEXT_CONVERSATION_HISTORY_BUDGET", 16000),
                    floor_chars=_env_int("CONTEXT_CONVERSATION_HISTORY_FLOOR", 4000),
                    strategy=os.getenv(
                        "CONTEXT_CONVERSATION_HISTORY_STRATEGY",
                        "summary_middle",
                    ),
                    keep_head_turns=_env_int("CONTEXT_HISTORY_KEEP_HEAD_TURNS", 3),
                    keep_tail_turns=_env_int("CONTEXT_HISTORY_KEEP_TAIL_TURNS", 6),
                    summary_chars=_env_int("CONTEXT_HISTORY_SUMMARY_MAX_CHARS", 3000),
                ),
                "active_turn": SectionBudgetRule(
                    name="active_turn",
                    budget_chars=_env_int("CONTEXT_ACTIVE_TURN_BUDGET", 8000),
                    floor_chars=_env_int("CONTEXT_ACTIVE_TURN_FLOOR", 3000),
                    strategy=os.getenv(
                        "CONTEXT_ACTIVE_TURN_STRATEGY",
                        "latest_tool_call",
                    ),
                    summary_chars=_env_int("CONTEXT_ACTIVE_TURN_SUMMARY_MAX_CHARS", 1800),
                    keep_recent_results=_env_int("CONTEXT_ACTIVE_TURN_KEEP_RECENT_RESULTS", 5),
                    preserve_tools=_env_list(
                        "CONTEXT_ACTIVE_TURN_PRESERVE_TOOLS",
                        (
                            "read_file",
                            "list_files",
                            "rg",
                            "git_diff",
                            "git_status",
                            "git_log",
                            "code_outline",
                            "repo_map",
                        ),
                    ),
                ),
            },
        )

    def apply(self, name: str, text: str, *, raw_text: str | None = None) -> BudgetedText:
        raw = str(text if raw_text is None else raw_text or "")
        rendered = str(text or "")
        rule = self.rules.get(name)
        if rule is None:
            return BudgetedText(name=name, raw_text=raw, rendered_text=rendered)

        target = max(1, int(rule.budget_chars))
        floor = max(0, int(rule.floor_chars))
        metadata = {
            "budget_enabled": self.enabled,
            "strategy": rule.strategy,
            "floor_chars": floor,
        }

        if not self.enabled:
            return BudgetedText(
                name=name,
                raw_text=raw,
                rendered_text=rendered,
                budget_chars=None,
                floor_chars=floor,
                strategy=rule.strategy,
                truncated=False,
                metadata=metadata,
            )

        if len(rendered) <= target:
            return BudgetedText(
                name=name,
                raw_text=raw,
                rendered_text=rendered,
                budget_chars=target,
                floor_chars=floor,
                strategy=rule.strategy,
                truncated=False,
                metadata=metadata,
            )

        effective_target = max(target, min(floor, len(rendered)))
        clipped = _clip(rendered, effective_target, strategy=rule.strategy)
        reduction = {
            "section": name,
            "reason": "section_budget",
            "before_chars": len(rendered),
            "after_chars": len(clipped),
            "budget_chars": target,
            "floor_chars": floor,
            "effective_budget_chars": effective_target,
            "strategy": rule.strategy,
        }
        return BudgetedText(
            name=name,
            raw_text=raw,
            rendered_text=clipped,
            budget_chars=target,
            floor_chars=floor,
            strategy=rule.strategy,
            truncated=True,
            reduction=reduction,
            metadata=metadata,
        )


def _clip(text: str, limit: int, *, strategy: str) -> str:
    if len(text) <= limit:
        return text
    marker = "\n...[truncated]"
    if limit <= len(marker) + 8:
        return text[:limit]

    if strategy == "tail":
        keep = limit - len(marker)
        return marker + text[-keep:].lstrip()

    if strategy == "head_tail":
        marker = "\n...[truncated]...\n"
        keep = max(0, limit - len(marker))
        head = keep // 2
        tail = keep - head
        return text[:head].rstrip() + marker + text[-tail:].lstrip()

    keep = limit - len(marker)
    return text[:keep].rstrip() + marker


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return int(default)
    try:
        return int(value)
    except ValueError:
        return int(default)


def _env_int_any(names: list[str], default: int) -> int:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            try:
                return int(value)
            except ValueError:
                return int(default)
    return int(default)


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None or value == "":
        return tuple(default)
    return tuple(item.strip() for item in value.split(",") if item.strip())
