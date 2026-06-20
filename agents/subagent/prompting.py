from __future__ import annotations

import json
import re
from typing import Any

from runtime.failure_reasons import INCOMPLETE_STEP_LIMIT_PREFIX


def subtask_prompt(*, prompt: str, agent_type: str, description: str) -> str:
    title = description.strip() or agent_type
    return (
        "<subtask>\n"
        f"Description: {title}\n"
        f"Agent type: {agent_type}\n\n"
        f"{prompt.strip()}\n"
        "</subtask>"
    )


def extract_structured_result(summary: str) -> dict[str, Any]:
    payload = _load_json_object(summary)
    if not payload:
        return _empty_structured_result()
    findings = payload.get("findings")
    if not isinstance(findings, list):
        findings = []
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    return {
        "findings": [item for item in findings if isinstance(item, dict)],
        "incomplete": bool(payload.get("incomplete")),
        "failure_reason": _optional_str(payload.get("failure_reason")),
        "failure_message": _optional_str(payload.get("failure_message")),
        "recoverable": payload.get("recoverable"),
        "retry_hint": _optional_str(payload.get("retry_hint")),
        "evidence": [item for item in evidence if isinstance(item, dict)],
        "scope_too_broad": bool(payload.get("scope_too_broad")),
        "status": _optional_str(payload.get("status")),
    }


def incomplete_summary(summary: str) -> str:
    summary = str(summary or "").strip()
    if summary.startswith(INCOMPLETE_STEP_LIMIT_PREFIX):
        return summary
    return INCOMPLETE_STEP_LIMIT_PREFIX + summary


def _load_json_object(text: str) -> dict[str, Any] | None:
    text = str(text or "").strip()
    if not text:
        return None
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1))
    for candidate in candidates:
        try:
            loaded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            return loaded
    return None


def _empty_structured_result() -> dict[str, Any]:
    return {
        "findings": [],
        "incomplete": False,
        "failure_reason": None,
        "failure_message": None,
        "recoverable": None,
        "retry_hint": None,
        "evidence": [],
        "scope_too_broad": False,
        "status": None,
    }


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
