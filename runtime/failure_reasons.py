from __future__ import annotations

from enum import StrEnum


class StopReason(StrEnum):
    REASONING_STEP_LIMIT = "reasoning_step_limit"
    EMPTY_MODEL_RESPONSE = "empty_model_response"
    REPEATED_TOOL_CALL = "repeated_tool_call"
    UNAVAILABLE_TOOL_LOOP = "unavailable_tool_loop"
    USER_CANCELLED = "user_cancelled"
    TIMEOUT = "timeout"


REASONING_LOOP_STOP_REASON_KEY = "reasoning_loop_stop_reason"
REASONING_LOOP_STOP_MESSAGE_KEY = "reasoning_loop_stop_message"

INCOMPLETE_STEP_LIMIT_PREFIX = "[INCOMPLETE: hit step limit] "


BUDGET_STOP_REASONS = {
    StopReason.REASONING_STEP_LIMIT.value,
}

LOOP_GUARD_STOP_REASONS = {
    StopReason.REPEATED_TOOL_CALL.value,
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
