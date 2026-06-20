from __future__ import annotations

from sessions import Session
from runtime.trace.run_state import RunState
from runtime.trace.trace_store import event_preview

from agents.subagent.result import SubagentResult


def subagent_trace_run_state(
    *,
    parent_run_state,
    session: Session,
    agent_type: str,
    description: str,
    subagent_span_id: str | None,
) -> RunState | None:
    if parent_run_state is None:
        return None
    return RunState.create(
        session_id=session.id,
        channel=getattr(parent_run_state, "channel", ""),
        chat_id=getattr(parent_run_state, "chat_id", ""),
        user_id=getattr(parent_run_state, "user_id", None),
        user_role=getattr(parent_run_state, "user_role", None),
        mode="coding",
        execution_path="subagent",
        intent="subagent",
        profile=f"subagent:{agent_type}",
        run_id=getattr(parent_run_state, "run_id", None),
        metadata={
            "kind": "subagent",
            "trace_only": True,
            "agent_type": agent_type,
            "description": description,
            "parent_run_id": getattr(parent_run_state, "run_id", ""),
            "parent_session_id": (
                (session.metadata or {}).get("parent_session_id")
                if isinstance(session.metadata, dict)
                else ""
            ),
            "parent_span_id": parent_span_id(subagent_span_id) or "",
            "trace_span_prefix": subagent_span_id or "",
        },
    )


def trace_subagent_started(
    trace_store,
    run_state: RunState | None,
    *,
    span_id: str | None,
    parent_span_id: str | None,
    prompt: str,
    agent_type: str,
    description: str,
) -> None:
    if trace_store is None or run_state is None:
        return
    trace_store.append_event(
        run_state,
        "subagent.started",
        {
            "agent_type": agent_type,
            "description": description,
            "prompt_preview": event_preview(prompt),
        },
        span_id=span_id,
        parent_span_id=parent_span_id,
        session_id=run_state.session_id,
    )


def trace_subagent_completed(
    trace_store,
    run_state: RunState | None,
    *,
    span_id: str | None,
    parent_span_id: str | None,
    result: SubagentResult,
) -> None:
    if trace_store is None or run_state is None:
        return
    trace_store.append_event(
        run_state,
        "subagent.completed",
        {
            "agent_type": result.agent_type,
            "success": result.success,
            "truncated": result.truncated,
            "incomplete": result.incomplete,
            "stop_reason": result.stop_reason,
            "status": result.status,
            "failure_reason": result.failure_reason,
            "failure_message": result.failure_message,
            "recoverable": result.recoverable,
            "retry_hint": result.retry_hint,
            "evidence": result.evidence,
            "tool_count": result.tool_count,
            "files_touched": result.files_touched,
            "findings": result.findings,
            "summary_preview": event_preview(result.summary),
            "error_preview": event_preview(result.error),
            "reasoning_steps": run_state.reasoning_steps,
            "tool_calls": run_state.tool_calls,
        },
        span_id=span_id,
        parent_span_id=parent_span_id,
        session_id=run_state.session_id,
    )


def subagent_span_id(parent_span_id: str | None) -> str | None:
    if not parent_span_id:
        return None
    if ":subagent:" in parent_span_id:
        return parent_span_id
    return f"{parent_span_id}:subagent:0"


def parent_span_id(span_id: str | None) -> str | None:
    if not span_id or ":subagent:" not in span_id:
        return None
    return span_id.rsplit(":subagent:", 1)[0]
