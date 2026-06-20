from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from runtime.failure_reasons import (
    StopReason,
    is_budget_stop_reason,
    is_loop_guard_stop_reason,
)


class FailureCategory(StrEnum):
    NONE = "none"
    PASS = "pass"

    PATCH_WRONG = "patch_wrong"
    TEST_FAILED = "test_failed"

    RUN_FAILED = "run_failed"
    MODEL_ERROR = "model_error"
    TOOL_ERROR = "tool_error"
    TOOL_DENIED = "tool_denied"
    LOOP_GUARD = "loop_guard"
    EMPTY_MODEL_RESPONSE = "empty_model_response"
    TIMEOUT = "timeout"

    INFRA_ERROR = "infra_error"
    DOCKER_ERROR = "docker_error"
    DEPENDENCY_ERROR = "dependency_error"
    NETWORK_ERROR = "network_error"

    WORKSPACE_VIOLATION = "workspace_violation"
    BUDGET_EXCEEDED = "budget_exceeded"
    TASK_DESIGN_ERROR = "task_design_error"
    VERIFIER_ERROR = "verifier_error"
    PROTOCOL_ERROR = "protocol_error"
    TRACE_MISSING = "trace_missing"
    WORKSPACE_DIFF_FAILED = "workspace_diff_failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FailureClassification:
    category: FailureCategory
    reason: str
    evidence: list[str]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["category"] = self.category.value
        return payload


def classify_failure(
    *,
    run_state: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    report: dict[str, Any] | None = None,
    external_logs: list[str] | None = None,
) -> FailureClassification:
    run_state = run_state or {}
    events = events or []
    report = report or {}
    logs = "\n".join(str(item) for item in (external_logs or []))
    combined_text = "\n".join(
        [
            logs,
            str(run_state.get("error") or ""),
            str(run_state.get("stop_reason") or ""),
            str(report.get("failure_reason") or ""),
            str(report.get("error") or ""),
        ]
    )

    if _looks_like_docker_error(combined_text):
        return FailureClassification(
            FailureCategory.DOCKER_ERROR,
            "Docker image build, pull, or container execution failed.",
            _evidence(["docker", "registry-1.docker.io", "ImageNotFound", "context deadline exceeded"], combined_text),
        )
    if _looks_like_network_error(combined_text):
        return FailureClassification(
            FailureCategory.NETWORK_ERROR,
            "Network access failed during model, dependency, or evaluation execution.",
            _evidence(["timeout", "deadline exceeded", "ConnectionError"], combined_text),
        )
    if _looks_like_dependency_error(combined_text):
        return FailureClassification(
            FailureCategory.DEPENDENCY_ERROR,
            "A required Python package or runtime dependency is missing.",
            _evidence(["ModuleNotFoundError", "ImportError", "No module named"], combined_text),
        )

    for event in events:
        name = str(event.get("event") or "")
        payload = event.get("payload") or {}
        if name == "empty_model_response":
            return FailureClassification(
                FailureCategory.EMPTY_MODEL_RESPONSE,
                "The model returned an empty response without tool calls.",
                [str(payload)[:500]],
            )
        if name == "reasoning_budget_exceeded":
            return FailureClassification(
                FailureCategory.BUDGET_EXCEEDED,
                "The reasoning loop exceeded its configured step budget.",
                [str(payload)[:500]],
            )
        if name == "run_stopped":
            reason = str(payload.get("reason") or "")
            if reason == StopReason.EMPTY_MODEL_RESPONSE.value:
                return FailureClassification(
                    FailureCategory.EMPTY_MODEL_RESPONSE,
                    "The run stopped after repeated empty model responses.",
                    [str(payload)[:500]],
                )
            if is_budget_stop_reason(reason) or is_loop_guard_stop_reason(reason):
                category = (
                    FailureCategory.BUDGET_EXCEEDED
                    if is_budget_stop_reason(reason)
                    else FailureCategory.LOOP_GUARD
                )
                return FailureClassification(
                    category,
                    f"The run stopped because {reason}.",
                    [str(payload)[:500]],
                )
        tool_classification = _classify_tool_event(event)
        if tool_classification is not None:
            return tool_classification

    if run_state.get("status") == "completed" and not _report_failed(report):
        return FailureClassification(
            FailureCategory.PASS,
            "Run completed without a classified failure.",
            [],
        )

    if not _trace_has_events(events):
        return FailureClassification(
            FailureCategory.TRACE_MISSING,
            "No trace events were available for classification.",
            [],
        )

    if run_state.get("status") == "failed":
        return FailureClassification(
            FailureCategory.RUN_FAILED,
            str(run_state.get("error") or "Run failed."),
            [],
        )

    if _report_failed(report):
        return _classify_report_failure(report)

    return FailureClassification(
        FailureCategory.UNKNOWN,
        "Failure category could not be determined from run state, trace, or report.",
        [],
    )


def _classify_tool_event(event: dict[str, Any]) -> FailureClassification | None:
    name = str(event.get("event") or "")
    if name not in {"tool.call.completed", "tool.call.failed"}:
        return None
    payload = event.get("payload") or {}
    hooks = list(payload.get("pre_hook_trace") or []) + list(payload.get("post_hook_trace") or [])
    output = str(payload.get("output_preview") or payload.get("error_message") or "")
    for hook in hooks:
        hook_name = str(hook.get("hook_name") or "")
        decision = str(hook.get("decision") or "")
        reason = str(hook.get("reason") or output or "")
        if decision != "deny":
            continue
        if hook_name == "tool_loop_guard":
            return FailureClassification(
                FailureCategory.LOOP_GUARD,
                "A repeated tool call was blocked by the loop guard.",
                [reason[:500]],
            )
        if hook_name in {"shell_workspace_scope", "file_write_scope"}:
            return FailureClassification(
                FailureCategory.WORKSPACE_VIOLATION,
                "A tool attempted to operate outside the active workspace.",
                [reason[:500]],
            )
        return FailureClassification(
            FailureCategory.TOOL_DENIED,
            f"Tool call was denied by hook {hook_name}.",
            [reason[:500]],
        )
    if name == "tool.call.failed" or str(payload.get("status") or "") == "error":
        return FailureClassification(
            FailureCategory.TOOL_ERROR,
            str(payload.get("error_message") or "Tool call failed."),
            [output[:500]] if output else [],
        )
    return None


def _classify_report_failure(report: dict[str, Any]) -> FailureClassification:
    reason = str(report.get("failure_reason") or "")
    category = str(report.get("failure_category") or "")
    if category:
        return FailureClassification(
            _category_or_unknown(category),
            reason or f"Report classified failure as {category}.",
            [],
        )
    if "verifier" in reason or "test" in reason:
        return FailureClassification(FailureCategory.TEST_FAILED, reason, [])
    return FailureClassification(FailureCategory.VERIFIER_ERROR, reason or "Verifier failed.", [])


def _category_or_unknown(value: str) -> FailureCategory:
    try:
        return FailureCategory(value)
    except ValueError:
        mapping = {
            "verifier_failed": FailureCategory.VERIFIER_ERROR,
            "workspace_diff_failed": FailureCategory.WORKSPACE_DIFF_FAILED,
            "trace_missing": FailureCategory.TRACE_MISSING,
            "budget_exceeded": FailureCategory.BUDGET_EXCEEDED,
            "run_failed": FailureCategory.RUN_FAILED,
        }
        return mapping.get(value, FailureCategory.UNKNOWN)


def _looks_like_docker_error(text: str) -> bool:
    lowered = text.lower()
    return "docker.errors" in lowered or "docker://" in lowered or "registry-1.docker.io" in lowered


def _looks_like_network_error(text: str) -> bool:
    lowered = text.lower()
    return "deadline exceeded" in lowered or "connectionerror" in lowered or "read timed out" in lowered


def _looks_like_dependency_error(text: str) -> bool:
    return "ModuleNotFoundError" in text or "No module named" in text or "ImportError" in text


def _evidence(needles: list[str], text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        if any(needle.lower() in line.lower() for needle in needles):
            lines.append(line.strip()[:500])
    return lines[:5]


def _report_failed(report: dict[str, Any]) -> bool:
    return bool(report.get("failure_category") or report.get("failure_reason"))


def _trace_has_events(events: list[dict[str, Any]]) -> bool:
    return bool(events)
