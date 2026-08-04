"""Stable execution stop and failure reasons."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class StopReason(StrEnum):
    COMPLETED = "completed"
    USER_CANCELLED = "user_cancelled"
    WAITING_USER = "waiting_user"
    HARD_BUDGET_EXCEEDED = "hard_budget_exceeded"
    SECURITY_BLOCKED = "security_blocked"
    TOOL_UNAVAILABLE = "tool_unavailable"
    NON_RETRYABLE_FAILURE = "non_retryable_failure"
    REPEATED_SIDE_EFFECT_RISK = "repeated_side_effect_risk"
    NO_PROGRESS = "no_progress"
    RECOVERY_REJECTED = "recovery_rejected"
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    COMPACTION_FAILED_FINAL = "compaction_failed_final"
    PARTIAL_RESULT_ACCEPTED = "partial_result_accepted"


@dataclass(frozen=True)
class StopDecision:
    reason: StopReason
    message: str
    triggering_event_id: str | None = None
    recovery_attempted: bool = False
    recoverable: bool = False
    task_state_version: int | None = None


INCOMPLETE_STEP_LIMIT_PREFIX = "[INCOMPLETE: hit step limit] "


BUDGET_STOP_REASONS = {
    StopReason.HARD_BUDGET_EXCEEDED.value,
}

LOOP_GUARD_STOP_REASONS = {
    StopReason.NO_PROGRESS.value,
    StopReason.REPEATED_SIDE_EFFECT_RISK.value,
}


def normalize_stop_reason(reason: str | StopReason | None) -> str:
    return str(reason or "")


def is_budget_stop_reason(reason: str | StopReason | None) -> bool:
    return normalize_stop_reason(reason) in BUDGET_STOP_REASONS


def is_loop_guard_stop_reason(reason: str | StopReason | None) -> bool:
    return normalize_stop_reason(reason) in LOOP_GUARD_STOP_REASONS


class SubagentFailureReason(StrEnum):
    STEP_LIMIT = "subagent_step_limit"
    TIMEOUT = "subagent_timeout"
    TOOL_ERROR = "subagent_tool_error"
    TOOL_DENIED = "subagent_tool_denied"
    EMPTY_FINDINGS = "subagent_empty_findings"
    SCOPE_TOO_BROAD = "subagent_scope_too_broad"
    MISSING_REQUIRED_FILES = "subagent_missing_required_files"
    MODEL_ERROR = "subagent_model_error"
    INVALID_OUTPUT_FORMAT = "subagent_invalid_output_format"
    UNKNOWN_AGENT_TYPE = "subagent_unknown_agent_type"
    INTERNAL_ERROR = "subagent_internal_error"
    INFEASIBLE = "subagent_infeasible"


SUBAGENT_AUTO_RETRY_REASONS = {
    SubagentFailureReason.INTERNAL_ERROR.value,
    SubagentFailureReason.TIMEOUT.value,
}

SUBAGENT_SEMANTIC_RETRY_REASONS = {
    SubagentFailureReason.STEP_LIMIT.value,
    SubagentFailureReason.TOOL_ERROR.value,
    SubagentFailureReason.SCOPE_TOO_BROAD.value,
    SubagentFailureReason.INVALID_OUTPUT_FORMAT.value,
}

SUBAGENT_TERMINAL_REASONS = {
    SubagentFailureReason.MISSING_REQUIRED_FILES.value,
    SubagentFailureReason.EMPTY_FINDINGS.value,
    SubagentFailureReason.INFEASIBLE.value,
}

SUBAGENT_DEGRADE_LADDER = (
    "narrow_subagent",
    "narrower_code_outline_subagents",
    "spawn_teammate",
    "parent_direct_or_incomplete",
)

SUBAGENT_DEGRADE_BUDGET = 2
