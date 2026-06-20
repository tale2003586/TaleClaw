from __future__ import annotations

import json
from pathlib import Path
from typing import Any


MAX_PREVIEW_CHARS = 1200


def write_markdown_report(run_dir: Path) -> Path:
    run_state = _read_json(run_dir / "run_state.json")
    metrics = _read_json(run_dir / "metrics.json")
    report = _read_json(run_dir / "report.json")
    events = _read_events(run_dir / "trace.jsonl")

    body = _render_report(
        run_state=run_state,
        metrics=metrics,
        report=report.get("report", {}),
        events=events,
    )
    path = run_dir / "report.md"
    path.write_text(body, encoding="utf-8")
    return path


def _render_report(
    *,
    run_state: dict[str, Any],
    metrics: dict[str, Any],
    report: dict[str, Any],
    events: list[dict[str, Any]],
) -> str:
    inbound = _first_payload(events, "inbound_received")
    route = _first_payload(events, "route_selected")
    context = _last_context_summary(events)
    sanitized = [
        event.get("payload") or {}
        for event in events
        if event.get("event") == "context.sanitized"
    ]
    model_events = [
        event for event in events
        if event.get("event") in {"model.call.completed", "model.call.failed"}
    ]
    tool_events = [
        event for event in events
        if event.get("event") in {"tool.call.completed", "tool.call.failed"}
    ]
    error_events = [
        event for event in events
        if str(event.get("event") or "").endswith(".failed")
        or event.get("event") == "run_failed"
    ]

    lines = [
        "# Run Report",
        "",
        "## Summary",
        "",
        f"- Run ID: `{_value(run_state.get('run_id'))}`",
        f"- Status: `{_value(run_state.get('status'))}`",
        f"- Mode: `{_value(run_state.get('mode'))}`",
        f"- Execution Path: `{_value(run_state.get('execution_path'))}`",
        f"- Intent: `{_value(run_state.get('intent'))}`",
        f"- Profile: `{_value(run_state.get('profile'))}`",
        f"- User: `{_value(run_state.get('user_id'))}` ({_value(run_state.get('user_role'))})",
        f"- Session: `{_value(run_state.get('session_id'))}`",
        f"- Started: {_value(run_state.get('started_at'))}",
        f"- Finished: {_value(run_state.get('finished_at'))}",
        f"- Duration: {_value(metrics.get('run_duration_ms'))} ms",
        f"- Reasoning Steps: {_value(run_state.get('reasoning_steps'))}",
        f"- Model Calls: {_value(metrics.get('model_calls'))} ok / {_value(metrics.get('model_failures'))} failed",
        f"- Tool Calls: {_value(metrics.get('tool_calls'))} total, {_value(metrics.get('tool_failures'))} failed, {_value(metrics.get('tool_denials'))} denied",
        f"- Duplicate Tool Call Ratio: {_value(metrics.get('duplicate_tool_call_ratio'))}",
        f"- Truncated Tool Outputs: {_value(metrics.get('truncated_tool_output_count'))}",
        f"- Subagent Incomplete: {_value(metrics.get('subagent_incomplete_count'))}",
        f"- Subagent Fan-out: {_value(metrics.get('subagent_fanout_count'))}",
        f"- Tokens: {_value(metrics.get('total_tokens'))} total ({_value(metrics.get('input_tokens'))} in / {_value(metrics.get('output_tokens'))} out)",
        f"- Sanitized Messages: {_value(metrics.get('sanitized_messages'))}",
        "",
        "## User Request",
        "",
        _block(inbound.get("content_preview") or "(not recorded)"),
        "",
        "## Route",
        "",
        f"- Intent: `{_value(route.get('intent'))}`",
        f"- Execution: `{_value(route.get('execution'))}`",
        f"- Profile: `{_value(route.get('profile'))}`",
        f"- Tool Mode: `{_value(route.get('tool_mode'))}`",
        f"- Confidence: {_value(route.get('confidence'))}",
        f"- Reason: {_value(route.get('reason'))}",
        "",
        "## Model Activity",
        "",
        _model_table(model_events),
        "",
        "## Tool Activity",
        "",
        _tool_table(tool_events),
        "",
        "## Context",
        "",
        _context_section(context, sanitized),
        "",
        "## Errors",
        "",
        _error_section(run_state, error_events),
        "",
        "## Final Answer",
        "",
        _block(run_state.get("final_answer") or report.get("reply") or "(no final answer recorded)"),
        "",
    ]
    return "\n".join(lines)


def _model_table(events: list[dict[str, Any]]) -> str:
    if not events:
        return "_No model calls recorded._"
    rows = [
        "| Step | Provider | Model | Status | Duration | Tokens |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for event in events:
        payload = event.get("payload") or {}
        usage = payload.get("usage") or {}
        status = "failed" if event.get("event") == "model.call.failed" else "completed"
        rows.append(
            "| "
            f"{_value(event.get('step'))} | "
            f"{_escape_cell(payload.get('provider'))} | "
            f"{_escape_cell(payload.get('model'))} | "
            f"{status} | "
            f"{_value(payload.get('duration_ms'))} ms | "
            f"{_value(usage.get('total_tokens'))} |"
        )
    return "\n".join(rows)


def _tool_table(events: list[dict[str, Any]]) -> str:
    if not events:
        return "_No tool calls recorded._"
    rows = [
        "| Step | Tool | Status | Duration | Output |",
        "| --- | --- | --- | --- | --- |",
    ]
    for event in events:
        payload = event.get("payload") or {}
        rows.append(
            "| "
            f"{_value(event.get('step'))} | "
            f"{_escape_cell(payload.get('tool_name'))} | "
            f"{_escape_cell(payload.get('status'))} | "
            f"{_value(payload.get('duration_ms'))} ms | "
            f"{_escape_cell(_preview(payload.get('output_preview')))} |"
        )
    return "\n".join(rows)


def _context_section(context: dict[str, Any], sanitized: list[dict[str, Any]]) -> str:
    if not context:
        return "_No context summary recorded._"
    roles = context.get("roles") or {}
    lines = [
        f"- Message Count: {_value(context.get('message_count'))}",
        f"- Estimated Tokens: {_value(context.get('estimated_tokens'))}",
        f"- Empty Assistant Messages: {_value(context.get('empty_assistant_messages'))}",
        f"- Role Breakdown: `{json.dumps(roles, ensure_ascii=False, default=str)}`",
    ]
    if sanitized:
        lines.append("")
        lines.append("### Sanitizer")
        lines.append("")
        for item in sanitized:
            lines.append(f"- Dropped: {_value(item.get('dropped_count'))}")
            for dropped in item.get("dropped_messages") or []:
                lines.append(
                    "  - "
                    f"index={_value(dropped.get('index'))}, "
                    f"role={_value(dropped.get('role'))}, "
                    f"reason={_value(dropped.get('reason'))}"
                )
    return "\n".join(lines)


def _error_section(run_state: dict[str, Any], events: list[dict[str, Any]]) -> str:
    lines = []
    if run_state.get("error"):
        lines.extend([
            "### Run Error",
            "",
            _block(run_state.get("error")),
            "",
        ])
    for event in events:
        payload = event.get("payload") or {}
        error_message = payload.get("error_message") or payload.get("error")
        if not error_message:
            continue
        lines.extend([
            f"### `{event.get('event')}`",
            "",
            f"- Step: {_value(event.get('step'))}",
            f"- Type: `{_value(payload.get('error_type'))}`",
            "",
            _block(error_message),
            "",
        ])
        attempts = payload.get("route_attempts") or payload.get("attempts") or []
        if attempts:
            lines.append("Route attempts:")
            lines.append("")
            for attempt in attempts:
                lines.append(
                    "- "
                    f"profile=`{_value(attempt.get('profile'))}`, "
                    f"provider=`{_value(attempt.get('provider'))}`, "
                    f"model=`{_value(attempt.get('model'))}`, "
                    f"status=`{_value(attempt.get('status'))}`"
                )
            lines.append("")
    return "\n".join(lines).strip() or "_No errors recorded._"


def _last_context_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        payload = event.get("payload") or {}
        summary = payload.get("context_summary")
        if isinstance(summary, dict):
            return summary
    return {}


def _first_payload(events: list[dict[str, Any]], event_name: str) -> dict[str, Any]:
    for event in events:
        if event.get("event") == event_name:
            payload = event.get("payload")
            return payload if isinstance(payload, dict) else {}
    return {}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _block(value: Any) -> str:
    return f"```text\n{_preview(value)}\n```"


def _preview(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= MAX_PREVIEW_CHARS:
        return text
    return text[:MAX_PREVIEW_CHARS] + "...[truncated]"


def _value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def _escape_cell(value: Any) -> str:
    return _value(value).replace("|", "\\|").replace("\n", " ")
