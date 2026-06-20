from __future__ import annotations

import traceback
import uuid

from agents.subagent.failure import (
    STATUS_FAILED,
    classify_subagent_failure,
    internal_error_failure,
    status_for_result,
    unknown_agent_type_failure,
)
from agents.subagent.inspection import count_tool_calls, extract_files_touched
from agents.subagent.prompting import (
    extract_structured_result,
    incomplete_summary,
    subtask_prompt,
)
from agents.subagent.result import SubagentResult
from agents.subagent.tools import (
    DEFAULT_SUBTASK_AGENT_TYPE,
    SUBTASK_SYSTEM_PROMPTS,
    SUBTASK_TOOL_WHITELIST,
)
from agents.subagent.trace import (
    parent_span_id as trace_parent_span_id,
    subagent_span_id as trace_subagent_span_id,
    subagent_trace_run_state,
    trace_subagent_completed,
    trace_subagent_started,
)
from config import SUBAGENT_MAX_REASONING_STEPS
from modes.base import ModeProfile
from runtime.context import ContextBuilder
from runtime.failure_reasons import (
    REASONING_LOOP_STOP_REASON_KEY,
    StopReason,
)
from runtime.pipeline import Pipeline, get_last_assistant_text
from sessions import Session
from tools.tool_registry import ToolRegistry


class TaskSubagentRunner:
    """Run a focused, short-lived subagent with isolated context."""

    def __init__(
        self,
        *,
        base_pipeline: Pipeline,
        max_reasoning_steps: int | None = None,
    ) -> None:
        self.base_pipeline = base_pipeline
        self.max_reasoning_steps = (
            max_reasoning_steps
            if max_reasoning_steps is not None
            else min(base_pipeline.max_reasoning_steps, SUBAGENT_MAX_REASONING_STEPS)
        )

    def run(
        self,
        *,
        prompt: str,
        agent_type: str = DEFAULT_SUBTASK_AGENT_TYPE,
        description: str = "",
        parent_session=None,
        trace_store=None,
        parent_run_state=None,
        parent_span_id: str | None = None,
    ) -> SubagentResult:
        requested_agent_type = agent_type
        agent_type = _normalize_agent_type(agent_type)
        if agent_type is None:
            failure = unknown_agent_type_failure(requested_agent_type)
            return SubagentResult(
                agent_type=str(requested_agent_type or ""),
                success=False,
                summary="",
                status=STATUS_FAILED,
                files_touched=[],
                tool_count=0,
                error=failure.message,
                incomplete=True,
                failure_reason=failure.reason,
                failure_message=failure.message,
                recoverable=failure.recoverable,
                retry_hint=failure.retry_hint,
                evidence=failure.evidence,
            )
        session = self._new_session(
            prompt=prompt,
            agent_type=agent_type,
            description=description,
            parent_session=parent_session,
        )
        pipeline = self._sub_pipeline(agent_type)
        profile = self._profile(agent_type)
        span_id = trace_subagent_span_id(parent_span_id)
        trace_run_state = subagent_trace_run_state(
            parent_run_state=parent_run_state,
            session=session,
            agent_type=agent_type,
            description=description,
            subagent_span_id=span_id,
        )

        try:
            trace_subagent_started(
                trace_store,
                trace_run_state,
                span_id=span_id,
                parent_span_id=trace_parent_span_id(span_id),
                prompt=prompt,
                agent_type=agent_type,
                description=description,
            )
            summary = pipeline.run(
                session,
                profile,
                run_state=trace_run_state,
                trace_store=trace_store,
                trace_parent_span_id=span_id,
            )
            stop_reason = _stop_reason(session)
            truncated = stop_reason == StopReason.REASONING_STEP_LIMIT.value
            if truncated:
                summary = incomplete_summary(summary or get_last_assistant_text(session.messages))
            summary_text = summary or get_last_assistant_text(session.messages)
            structured = extract_structured_result(summary_text)
            failure = classify_subagent_failure(
                session_messages=session.messages,
                stop_reason=stop_reason,
                structured=structured,
                truncated=truncated,
            )
            findings = structured.get("findings") or []
            incomplete = bool(truncated or structured.get("incomplete") or failure)
            success = not incomplete and failure is None
            result = SubagentResult(
                agent_type=agent_type,
                success=success,
                summary=summary_text,
                status=status_for_result(
                    success=success,
                    incomplete=incomplete,
                    findings=findings,
                    failure=failure,
                ),
                files_touched=extract_files_touched(session.messages),
                tool_count=count_tool_calls(session.messages),
                error=failure.message if failure else None,
                truncated=truncated,
                stop_reason=stop_reason,
                findings=findings,
                incomplete=incomplete,
                failure_reason=failure.reason if failure else None,
                failure_message=failure.message if failure else None,
                recoverable=failure.recoverable if failure else False,
                retry_hint=failure.retry_hint if failure else None,
                evidence=failure.evidence if failure else [],
            )
            trace_subagent_completed(
                trace_store,
                trace_run_state,
                span_id=span_id,
                parent_span_id=trace_parent_span_id(span_id),
                result=result,
            )
            return result
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            failure = internal_error_failure(error)
            result = SubagentResult(
                agent_type=agent_type,
                success=False,
                summary="",
                status=STATUS_FAILED,
                files_touched=extract_files_touched(session.messages),
                tool_count=count_tool_calls(session.messages),
                error=error,
                truncated=False,
                stop_reason=_stop_reason(session),
                findings=[],
                incomplete=True,
                failure_reason=failure.reason,
                failure_message=failure.message,
                recoverable=failure.recoverable,
                retry_hint=failure.retry_hint,
                evidence=failure.evidence,
            )
            trace_subagent_completed(
                trace_store,
                trace_run_state,
                span_id=span_id,
                parent_span_id=trace_parent_span_id(span_id),
                result=result,
            )
            return result

    def _sub_pipeline(self, agent_type: str) -> Pipeline:
        base_runner = self.base_pipeline.agent_runner
        return Pipeline(
            tools=self._filtered_tools(agent_type),
            provider=base_runner.provider,
            model=base_runner.model,
            tool_executor=base_runner.tool_executor,
            context_builder=ContextBuilder(
                memory_store=None,
                security_auto_context_enabled=False,
            ),
            memory_lifecycle=None,
            model_pool=base_runner.model_pool,
            reflection_agent=base_runner.reflection_agent,
            max_tokens=base_runner.max_tokens,
            max_reasoning_steps=self.max_reasoning_steps,
        )

    def _filtered_tools(self, agent_type: str) -> ToolRegistry:
        allowed = SUBTASK_TOOL_WHITELIST.get(agent_type, set())
        registry = ToolRegistry()
        for name, tool in self.base_pipeline.agent_runner.tools._tools.items():
            if name not in allowed:
                continue
            registry.register(
                tool.schema,
                tool.handler,
                risk=tool.risk,
                enabled_modes=set(tool.enabled_modes) if tool.enabled_modes else None,
                source=f"subagent:{agent_type}",
                always_on=tool.always_on,
                session_scoped=tool.session_scoped,
                admin_only=tool.admin_only,
            )
        return registry

    def _profile(self, agent_type: str) -> ModeProfile:
        return ModeProfile(
            name=f"subagent:{agent_type}",
            tool_mode="coding",
            system_prompt=SUBTASK_SYSTEM_PROMPTS[agent_type],
        )

    def _new_session(
        self,
        *,
        prompt: str,
        agent_type: str,
        description: str,
        parent_session=None,
    ) -> Session:
        metadata = {
            "kind": "subagent",
            "agent_type": agent_type,
            "description": description,
            "user_role": "admin",
        }
        if parent_session is not None:
            metadata["parent_session_id"] = getattr(parent_session, "id", "")
            parent_metadata = getattr(parent_session, "metadata", {}) or {}
            for key in (
                "user_id",
                "user_role",
                "workspace_root",
                "workspace_display_name",
                "workspace_allowed_root",
                "workspace_source",
                "workspace_requested",
            ):
                if key in parent_metadata:
                    metadata[key] = parent_metadata[key]
        session = Session(
            id=f"subtask:{agent_type}:{uuid.uuid4().hex[:8]}",
            current_mode="coding",
            metadata=metadata,
        )
        session.add_message(
            "user",
            subtask_prompt(prompt=prompt, agent_type=agent_type, description=description),
            metadata={"kind": "subtask_prompt"},
        )
        return session


def _normalize_agent_type(agent_type: str | None) -> str | None:
    value = (agent_type or "").strip() or DEFAULT_SUBTASK_AGENT_TYPE
    if value not in SUBTASK_TOOL_WHITELIST:
        return None
    return value


def _stop_reason(session: Session) -> str | None:
    value = (getattr(session, "metadata", {}) or {}).get(REASONING_LOOP_STOP_REASON_KEY)
    return str(value) if value else None
