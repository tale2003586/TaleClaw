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
from config import SUBAGENT_MAX_REASONING_STEPS, WORKING_MEMORY_CHECKPOINT_ENABLED
from runtime.context import ContextBuilder
from runtime.execution.failure_reasons import (
    REASONING_LOOP_STOP_REASON_KEY,
    StopReason,
)
from runtime.runtime import get_last_assistant_text
from runtime.agent_spec import AgentSpec, SpawnPolicy, ToolSet
from runtime.runtime import RunContext, Runtime
from runtime.execution.child_run import ChildRun
from runtime.working_memory import (
    inherit_working_memory,
    load_working_memory,
    save_working_memory,
)
from runtime.sessions import Session
from tools.tool_registry import ToolRegistry

STEP_LIMIT_SUMMARY_MAX_TOKENS = 1600


class TaskSubagentRunner:
    """Run a focused, short-lived subagent with isolated context."""

    def __init__(
        self,
        *,
        base_pipeline: Runtime,
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
        child_run = ChildRun.create(parent_run_state)

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
            agent_spec = profile
            summary = pipeline.run(
                agent_spec,
                prompt,
                RunContext(
                    session=session,
                    profile=profile,
                    run_state=trace_run_state,
                    trace_store=trace_store,
                    trace_parent_span_id=span_id,
                    state=child_run.execution_state(),
                ),
            ).output
            stop_reason = _stop_reason(session)
            truncated = stop_reason == StopReason.REASONING_STEP_LIMIT.value
            summary_text = summary or get_last_assistant_text(session.messages)
            if truncated:
                recovered_summary = self._summarize_after_step_limit(
                    pipeline=pipeline,
                    session=session,
                    profile=profile,
                )
                if recovered_summary:
                    summary_text = recovered_summary
            structured = extract_structured_result(summary_text, agent_type=agent_type)
            result_summary = str(structured.get("summary") or summary_text or "")
            if truncated:
                result_summary = incomplete_summary(result_summary)
            failure = classify_subagent_failure(
                session_messages=session.messages,
                stop_reason=stop_reason,
                structured=structured,
                truncated=truncated,
            )
            findings = structured.get("findings") or []
            payload = (
                structured.get("payload")
                if isinstance(structured.get("payload"), dict)
                else {}
            )
            incomplete = bool(truncated or structured.get("incomplete") or failure)
            success = not incomplete and failure is None
            result = SubagentResult(
                agent_type=agent_type,
                success=success,
                summary=result_summary,
                status=status_for_result(
                    success=success,
                    incomplete=incomplete,
                    findings=findings,
                    failure=failure,
                    payload=payload,
                ),
                output_schema=str(structured.get("output_schema") or ""),
                payload=payload,
                format_valid=bool(structured.get("format_valid", True)),
                format_error=str(structured.get("format_error") or ""),
                format_repaired=bool(structured.get("format_repaired")),
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
                evidence=failure.evidence if failure else structured.get("evidence") or [],
                covered_scope=structured.get("covered_scope") or [],
                open_questions=structured.get("open_questions") or [],
                needs_parent_verification=bool(structured.get("needs_parent_verification")),
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

    def _sub_pipeline(self, agent_type: str) -> Runtime:
        base_runner = self.base_pipeline.agent_runner
        return Runtime(
            tools=self._filtered_tools(agent_type),
            provider=base_runner.provider,
            model=base_runner.model,
            tool_executor=base_runner.tool_executor,
            context_builder=ContextBuilder(),
            memory_lifecycle=None,
            model_pool=base_runner.model_pool,
            reflection_agent=base_runner.reflection_agent,
            max_tokens=base_runner.max_tokens,
            max_reasoning_steps=self.max_reasoning_steps,
        )

    def _summarize_after_step_limit(
        self,
        *,
        pipeline: Runtime,
        session: Session,
        profile: AgentSpec,
    ) -> str:
        session.add_message(
            "user",
            _step_limit_summary_prompt(self.max_reasoning_steps),
            metadata={
                "kind": "subagent_step_limit_summary_request",
                "reason": StopReason.REASONING_STEP_LIMIT.value,
            },
        )
        try:
            context = pipeline.agent_runner.context_builder.build(
                session=session,
                profile=profile,
                include_security_knowledge=False,
            )
            provider, model = pipeline.provider_and_model_for("summary")
            response = provider.chat(
                model=model,
                messages=getattr(context, "messages", []),
                tools=[],
                tool_choice="none",
                max_tokens=min(
                    max(1, int(pipeline.max_tokens)),
                    STEP_LIMIT_SUMMARY_MAX_TOKENS,
                ),
            )
        except Exception as exc:
            session.metadata["subagent_step_limit_summary_error"] = (
                f"{type(exc).__name__}: {exc}"
            )
            return ""
        content = str(response.content or "").strip()
        if not content:
            session.metadata["subagent_step_limit_summary_error"] = "empty_summary"
            return ""
        session.add_message(
            "assistant",
            content,
            metadata={
                "kind": "subagent_step_limit_summary",
                "reason": StopReason.REASONING_STEP_LIMIT.value,
            },
        )
        session.metadata["subagent_step_limit_summary_used"] = True
        return content

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
                allowed_agents=set(tool.allowed_agents) if tool.allowed_agents else None,
                source=f"subagent:{agent_type}",
                always_on=tool.always_on,
                session_scoped=tool.session_scoped,
                admin_only=tool.admin_only,
            )
        return registry

    def _profile(self, agent_type: str) -> AgentSpec:
        return AgentSpec(
            name=f"subagent:{agent_type}",
            role="subagent",
            instructions=SUBTASK_SYSTEM_PROMPTS[agent_type],
            tool_set=ToolSet(mode="coding"),
            spawn_policy=SpawnPolicy(enabled=False),
            metadata={"agent_type": agent_type},
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
            active_agent="coding",
            metadata=metadata,
        )
        if WORKING_MEMORY_CHECKPOINT_ENABLED and parent_session is not None:
            inherit_working_memory(
                source_session=parent_session,
                target_session=session,
                objective=prompt,
                task_id=session.id,
                include_pending_units=False,
            )
            memory = load_working_memory(session)
            if memory is not None:
                memory.task_id = session.id
                memory.objective = prompt
                memory.archived_findings["inherited_parent_working_memory"] = {
                    "parent_session_id": getattr(parent_session, "id", ""),
                    "description": description,
                    "agent_type": agent_type,
                    "mode": "snapshot",
                }
                save_working_memory(session, memory)
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


def _step_limit_summary_prompt(max_steps: int) -> str:
    return (
        "<subagent-step-limit-summary>\n"
        f"You hit the subagent reasoning step limit ({max_steps}). Do not call tools. "
        "Do not continue investigation. Summarize only the work already completed "
        "from the conversation and tool results above so the parent agent can reuse "
        "the partial progress.\n\n"
        "Return JSON only with this shape:\n"
        "{\"schema_version\":\"subagent.explore.v1\",\"agent_type\":\"explore\","
        "\"status\":\"partial\",\"summary\":\"short summary of completed work\","
        "\"payload\":{\"findings\":[{\"claim\":\"reusable fact already supported\","
        "\"path\":\"relative/file.py\",\"lines\":\"1-20\","
        "\"entry\":\"symbol_or_section\",\"evidence\":\"short observed signal\","
        "\"confidence\":\"high|medium|low\","
        "\"needs_parent_verification\":true}],"
        "\"evidence\":[{\"path\":\"relative/file.py\",\"lines\":\"1-20\","
        "\"quote_or_signal\":\"...\"}],"
        "\"covered_scope\":[\"relative/file.py\"],"
        "\"open_questions\":[\"specific unfinished item\"],"
        "\"needs_parent_verification\":true},"
        "\"incomplete\":true,"
        "\"failure_reason\":\"subagent_step_limit\","
        "\"failure_message\":\"Hit the reasoning step limit after partial progress.\","
        "\"recoverable\":true,"
        "\"retry_hint\":\"Retry with a narrower scope or continue from the listed open_questions.\"}\n\n"
        "If no reliable file-backed findings exist, return findings=[] and evidence=[], "
        "but still fill open_questions and retry_hint.\n"
        "</subagent-step-limit-summary>"
    )
