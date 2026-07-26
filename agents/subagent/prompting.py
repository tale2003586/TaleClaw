from __future__ import annotations

from typing import Any

from runtime.execution.failure_reasons import INCOMPLETE_STEP_LIMIT_PREFIX
from runtime.units.json_repair import repair_json_object


OUTPUT_SCHEMA_BY_AGENT_TYPE = {
    "explore": "subagent.explore.v1",
    "plan": "subagent.plan.v1",
    "code": "subagent.code.v1",
}


def subtask_prompt(*, prompt: str, agent_type: str, description: str) -> str:
    title = description.strip() or agent_type
    return (
        "<subtask>\n"
        f"Description: {title}\n"
        f"Agent type: {agent_type}\n\n"
        f"{prompt.strip()}\n"
        "</subtask>"
    )


def extract_structured_result(summary: str, *, agent_type: str = "explore") -> dict[str, Any]:
    schema = output_schema_for(agent_type)
    repaired = repair_json_object(summary)
    if not repaired.ok or not isinstance(repaired.payload, dict):
        result = _empty_structured_result(agent_type=agent_type, output_schema=schema)
        raw_text = repaired.text or str(summary or "")
        result.update({
            "format_valid": False,
            "format_error": repaired.error or "invalid_json_object",
            "format_repaired": False,
            "raw_text": raw_text,
            "summary": raw_text,
        })
        return result

    envelope = repaired.payload
    payload = _payload_from_envelope(envelope)
    common = _common_fields(envelope, payload)
    normalized_payload = _normalize_payload(agent_type, payload)
    compatibility = _compatibility_fields(agent_type, normalized_payload, envelope)
    return {
        **_empty_structured_result(agent_type=agent_type, output_schema=schema),
        **common,
        **compatibility,
        "payload": normalized_payload,
        "format_valid": True,
        "format_error": "",
        "format_repaired": bool(repaired.repaired),
        "raw_payload": envelope,
        "raw_text": str(summary or ""),
        "output_schema": schema,
    }


def output_schema_for(agent_type: str) -> str:
    return OUTPUT_SCHEMA_BY_AGENT_TYPE.get(str(agent_type or "explore"), "subagent.generic.v1")


def incomplete_summary(summary: str) -> str:
    summary = str(summary or "").strip()
    if summary.startswith(INCOMPLETE_STEP_LIMIT_PREFIX):
        return summary
    return INCOMPLETE_STEP_LIMIT_PREFIX + summary


def _payload_from_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    payload = envelope.get("payload")
    if isinstance(payload, dict):
        return payload
    return envelope


def _common_fields(envelope: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": _optional_str(_first_nonempty(envelope.get("summary"), payload.get("summary"))),
        "status": _optional_str(_first_nonempty(envelope.get("status"), payload.get("status"))),
        "incomplete": bool(_first_nonempty(envelope.get("incomplete"), payload.get("incomplete"), False)),
        "failure_reason": _optional_str(_first_nonempty(envelope.get("failure_reason"), payload.get("failure_reason"))),
        "failure_message": _optional_str(_first_nonempty(envelope.get("failure_message"), payload.get("failure_message"))),
        "recoverable": _first_nonempty(envelope.get("recoverable"), payload.get("recoverable")),
        "retry_hint": _optional_str(_first_nonempty(envelope.get("retry_hint"), payload.get("retry_hint"))),
        "scope_too_broad": bool(_first_nonempty(envelope.get("scope_too_broad"), payload.get("scope_too_broad"), False)),
    }


def _normalize_payload(agent_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if agent_type == "plan":
        return {
            "plan": _list_of_dict_or_str(payload.get("plan")),
            "risks": _list_of_dict_or_str(payload.get("risks")),
            "dependencies": _list_of_dict_or_str(payload.get("dependencies")),
            "evidence": _list_of_dicts(payload.get("evidence")),
            "open_questions": _list_of_str(payload.get("open_questions")),
            "covered_scope": _list_of_str(payload.get("covered_scope")),
        }
    if agent_type == "code":
        return {
            "changes": _list_of_dict_or_str(payload.get("changes")),
            "files_touched": _list_of_str(payload.get("files_touched")),
            "tests": _list_of_dict_or_str(payload.get("tests")),
            "verification": _list_of_dict_or_str(payload.get("verification")),
            "risk": _optional_str(payload.get("risk")) or "",
            "evidence": _list_of_dicts(payload.get("evidence")),
            "open_questions": _list_of_str(payload.get("open_questions")),
        }
    return {
        "findings": _list_of_dicts(payload.get("findings")),
        "evidence": _list_of_dicts(payload.get("evidence")),
        "covered_scope": _list_of_str(payload.get("covered_scope")),
        "open_questions": _list_of_str(payload.get("open_questions")),
        "needs_parent_verification": bool(payload.get("needs_parent_verification")),
    }


def _compatibility_fields(
    agent_type: str,
    payload: dict[str, Any],
    envelope: dict[str, Any],
) -> dict[str, Any]:
    evidence = _list_of_dicts(payload.get("evidence") or envelope.get("evidence"))
    if agent_type == "explore":
        findings = _list_of_dicts(payload.get("findings"))
        covered_scope = _list_of_str(payload.get("covered_scope"))
        open_questions = _list_of_str(payload.get("open_questions"))
        needs_parent_verification = bool(payload.get("needs_parent_verification"))
    else:
        findings = []
        covered_scope = _list_of_str(payload.get("covered_scope"))
        open_questions = _list_of_str(payload.get("open_questions"))
        needs_parent_verification = False
    return {
        "findings": findings,
        "evidence": evidence,
        "covered_scope": covered_scope,
        "open_questions": open_questions,
        "needs_parent_verification": needs_parent_verification,
    }


def _empty_structured_result(*, agent_type: str, output_schema: str) -> dict[str, Any]:
    return {
        "agent_type": agent_type,
        "output_schema": output_schema,
        "payload": {},
        "raw_payload": {},
        "raw_text": "",
        "format_valid": True,
        "format_error": "",
        "format_repaired": False,
        "summary": None,
        "findings": [],
        "incomplete": False,
        "failure_reason": None,
        "failure_message": None,
        "recoverable": None,
        "retry_hint": None,
        "evidence": [],
        "covered_scope": [],
        "open_questions": [],
        "needs_parent_verification": False,
        "scope_too_broad": False,
        "status": None,
    }


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _list_of_str(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _list_of_dict_or_str(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            result.append(item)
        else:
            text = str(item).strip()
            if text:
                result.append(text)
    return result
