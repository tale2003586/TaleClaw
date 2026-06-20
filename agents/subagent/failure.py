from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from runtime.failure_reasons import StopReason, SubagentFailureReason


STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"


@dataclass
class SubagentFailure:
    reason: str
    message: str
    recoverable: bool = False
    retry_hint: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def is_partial(self, findings: list[dict[str, Any]] | None = None) -> bool:
        return bool(findings)

    def status_for(self, findings: list[dict[str, Any]] | None = None) -> str:
        if self.is_partial(findings):
            return STATUS_PARTIAL
        return STATUS_FAILED


def unknown_agent_type_failure(agent_type: Any) -> SubagentFailure:
    return SubagentFailure(
        reason=SubagentFailureReason.UNKNOWN_AGENT_TYPE.value,
        message=f"Unknown agent_type: {agent_type}",
        recoverable=True,
        retry_hint="Use one of the configured subagent types before dispatching again.",
        evidence=[{"agent_type": str(agent_type or "")}],
    )


def timeout_failure(timeout_seconds: float) -> SubagentFailure:
    return SubagentFailure(
        reason=SubagentFailureReason.TIMEOUT.value,
        message=f"Subagent task exceeded {timeout_seconds:g}s.",
        recoverable=True,
        retry_hint="Retry with a narrower scope, fewer files, or a smaller deliverable.",
        evidence=[{"timeout_seconds": timeout_seconds}],
    )


def internal_error_failure(error: str, *, evidence: list[dict[str, Any]] | None = None) -> SubagentFailure:
    return SubagentFailure(
        reason=SubagentFailureReason.INTERNAL_ERROR.value,
        message=_first_line(error) or "Subagent failed with an internal error.",
        recoverable=False,
        retry_hint="Inspect the exception before retrying the same task.",
        evidence=evidence or [],
    )


def classify_subagent_failure(
    *,
    session_messages: list[dict[str, Any]],
    stop_reason: str | None,
    structured: dict[str, Any] | None = None,
    truncated: bool = False,
) -> SubagentFailure | None:
    structured = structured or {}
    structured_reason = _structured_failure_reason(structured)
    if structured_reason:
        return _structured_failure(structured_reason, structured)

    if truncated or stop_reason == StopReason.REASONING_STEP_LIMIT.value:
        return SubagentFailure(
            reason=SubagentFailureReason.STEP_LIMIT.value,
            message="Subagent hit the reasoning step limit before completing the task.",
            recoverable=True,
            retry_hint="Split the task into a smaller scope with explicit files and a concrete deliverable.",
            evidence=[{"stop_reason": stop_reason or StopReason.REASONING_STEP_LIMIT.value}],
        )

    if stop_reason == StopReason.EMPTY_MODEL_RESPONSE.value:
        return SubagentFailure(
            reason=SubagentFailureReason.MODEL_ERROR.value,
            message="The model returned an empty response without a usable answer.",
            recoverable=True,
            retry_hint="Retry once with a shorter task prompt and an explicit output format.",
            evidence=[{"stop_reason": stop_reason}],
        )

    if stop_reason in {
        StopReason.REPEATED_TOOL_CALL.value,
        StopReason.UNAVAILABLE_TOOL_LOOP.value,
    }:
        return SubagentFailure(
            reason=SubagentFailureReason.TOOL_ERROR.value,
            message="Subagent was stopped by a tool loop guard.",
            recoverable=True,
            retry_hint="Retry with a narrower task and tell the subagent not to repeat the same tool arguments.",
            evidence=[{"stop_reason": stop_reason}],
        )

    tool_failure = _classify_tool_failure(session_messages)
    if tool_failure is not None:
        return tool_failure

    if structured.get("incomplete") and not structured.get("findings"):
        return SubagentFailure(
            reason=SubagentFailureReason.EMPTY_FINDINGS.value,
            message="Subagent marked the task incomplete and returned no findings.",
            recoverable=True,
            retry_hint="Retry with explicit target files or ask the parent agent to gather a file list first.",
            evidence=[],
        )

    return None


def status_for_result(
    *,
    success: bool,
    incomplete: bool,
    findings: list[dict[str, Any]] | None,
    failure: SubagentFailure | None,
) -> str:
    if success and not incomplete and failure is None:
        return STATUS_COMPLETED
    if failure is not None:
        return failure.status_for(findings)
    if incomplete and findings:
        return STATUS_PARTIAL
    return STATUS_FAILED


def _structured_failure_reason(structured: dict[str, Any]) -> str:
    value = structured.get("failure_reason")
    if value:
        return str(value)
    if structured.get("incomplete") and structured.get("scope_too_broad"):
        return SubagentFailureReason.SCOPE_TOO_BROAD.value
    return ""


def _structured_failure(reason: str, structured: dict[str, Any]) -> SubagentFailure:
    normalized = _normalize_reason(reason)
    default_message, default_hint, recoverable = _defaults_for_reason(normalized)
    return SubagentFailure(
        reason=normalized,
        message=str(structured.get("failure_message") or default_message),
        recoverable=_bool_or_default(structured.get("recoverable"), recoverable),
        retry_hint=(
            str(structured.get("retry_hint"))
            if structured.get("retry_hint")
            else default_hint
        ),
        evidence=_evidence_list(structured.get("evidence")),
    )


def _classify_tool_failure(messages: list[dict[str, Any]]) -> SubagentFailure | None:
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        status = str(message.get("status") or "")
        content = str(message.get("content") or "")
        evidence = [_tool_evidence(message)]
        if status == "denied":
            return SubagentFailure(
                reason=SubagentFailureReason.TOOL_DENIED.value,
                message=_first_line(content) or "A tool call was denied.",
                recoverable=True,
                retry_hint="Retry with allowed tool arguments or let the parent agent handle this step.",
                evidence=evidence,
            )
        if _looks_like_missing_file(content):
            return SubagentFailure(
                reason=SubagentFailureReason.MISSING_REQUIRED_FILES.value,
                message=_first_line(content) or "A required file or path was not available.",
                recoverable=True,
                retry_hint="List or search the workspace first, then retry with verified paths.",
                evidence=evidence,
            )
        if status == "error":
            return SubagentFailure(
                reason=SubagentFailureReason.TOOL_ERROR.value,
                message=_tool_error_message(message) or "A tool call failed.",
                recoverable=True,
                retry_hint="Retry only after changing the tool arguments or narrowing the task.",
                evidence=evidence,
            )
    return None


def _tool_evidence(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_call_id": str(message.get("tool_call_id") or ""),
        "status": str(message.get("status") or ""),
        "error_type": str(message.get("error_type") or ""),
        "error_message": str(message.get("error_message") or ""),
        "execution_error": str(message.get("execution_error") or ""),
        "final_arguments": (
            message.get("final_arguments")
            if isinstance(message.get("final_arguments"), dict)
            else {}
        ),
        "content_preview": _preview(message.get("content")),
    }


def _normalize_reason(reason: str) -> str:
    value = str(reason or "").strip()
    if not value:
        return SubagentFailureReason.INTERNAL_ERROR.value
    allowed = {item.value for item in SubagentFailureReason}
    if value in allowed:
        return value
    prefixed = f"subagent_{value}"
    if prefixed in allowed:
        return prefixed
    return SubagentFailureReason.INTERNAL_ERROR.value


def _defaults_for_reason(reason: str) -> tuple[str, str | None, bool]:
    if reason == SubagentFailureReason.STEP_LIMIT.value:
        return (
            "Subagent hit the reasoning step limit before completing the task.",
            "Split the task into a smaller scope with explicit files and a concrete deliverable.",
            True,
        )
    if reason == SubagentFailureReason.SCOPE_TOO_BROAD.value:
        return (
            "Subagent reported that the assigned scope is too broad.",
            "Retry with fewer files, a narrower module, or one question at a time.",
            True,
        )
    if reason == SubagentFailureReason.EMPTY_FINDINGS.value:
        return (
            "Subagent returned no findings.",
            "Provide verified files or search terms before retrying.",
            True,
        )
    if reason == SubagentFailureReason.MISSING_REQUIRED_FILES.value:
        return (
            "A required file or path was not available.",
            "List or search the workspace first, then retry with verified paths.",
            True,
        )
    if reason == SubagentFailureReason.TOOL_DENIED.value:
        return (
            "A tool call was denied.",
            "Retry with allowed tool arguments or let the parent agent handle this step.",
            True,
        )
    if reason == SubagentFailureReason.TOOL_ERROR.value:
        return (
            "A tool call failed.",
            "Retry only after changing the tool arguments or narrowing the task.",
            True,
        )
    if reason == SubagentFailureReason.TIMEOUT.value:
        return (
            "Subagent task timed out.",
            "Retry with a narrower scope or shorter timeout-sensitive task.",
            True,
        )
    if reason == SubagentFailureReason.MODEL_ERROR.value:
        return (
            "The model did not produce a usable answer.",
            "Retry once with a shorter prompt and explicit output format.",
            True,
        )
    if reason == SubagentFailureReason.UNKNOWN_AGENT_TYPE.value:
        return (
            "Unknown subagent type.",
            "Use one of the configured subagent types before dispatching again.",
            True,
        )
    if reason == SubagentFailureReason.INFEASIBLE.value:
        return (
            "Subagent reported that the clue is infeasible under the current constraints.",
            "Do not retry this subagent; record the blocker and continue other clues.",
            False,
        )
    return (
        "Subagent failed with an internal error.",
        "Inspect the exception before retrying the same task.",
        False,
    )


def _evidence_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _bool_or_default(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return default


def _looks_like_missing_file(content: str) -> bool:
    first = _first_line(content).lower()
    if not first.startswith(("error:", "tool error:", "filenotfounderror")):
        return False
    markers = [
        "path escapes workspace",
        "no such file",
        "filenotfounderror",
        "file not found",
        "not found",
        "does not exist",
    ]
    return any(marker in first for marker in markers)


def _tool_error_message(message: dict[str, Any]) -> str:
    for key in ("error_message", "execution_error"):
        value = _first_line(message.get(key))
        if value:
            return value
    return _first_line(message.get("content"))


def _first_line(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.splitlines()[0][:240]


def _preview(value: Any, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"
