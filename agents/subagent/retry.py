from __future__ import annotations

from typing import Any

from runtime.failure_reasons import SUBAGENT_AUTO_RETRY_REASONS


MAX_AUTO_RETRIES_PER_TASK = 1


def should_auto_retry(result: dict[str, Any], *, retry_count: int = 0) -> bool:
    if retry_count >= MAX_AUTO_RETRIES_PER_TASK:
        return False
    if bool(result.get("success")):
        return False
    reason = str(result.get("failure_reason") or "")
    return reason in SUBAGENT_AUTO_RETRY_REASONS


def annotate_retry_result(
    result: dict[str, Any],
    *,
    retry_count: int,
    initial_failure_reason: str,
) -> dict[str, Any]:
    annotated = dict(result)
    annotated["retry_count"] = retry_count
    annotated["auto_retry"] = retry_count > 0
    if initial_failure_reason:
        annotated["initial_failure_reason"] = initial_failure_reason
    if retry_count > 0 and bool(annotated.get("success")):
        annotated["recovered"] = True
        annotated["recovered_from_failure_reason"] = initial_failure_reason
    else:
        annotated["recovered"] = False
    return annotated
