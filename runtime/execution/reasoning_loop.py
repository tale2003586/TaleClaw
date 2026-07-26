"""Model and tool reasoning lifecycle."""

import inspect
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from runtime.execution.policy_set import ExecutionPolicies
from runtime.execution.failure_reasons import (
    INCOMPLETE_STEP_LIMIT_PREFIX,
    REASONING_LOOP_STOP_MESSAGE_KEY,
    REASONING_LOOP_STOP_REASON_KEY,
    StopReason,
)
from runtime.execution.message_sanitizer import (
    is_empty_assistant_message,
    sanitize_context_messages,
)
from runtime.execution.model_invocation import invoke_model, supports_streaming
from runtime.execution.tool_batch import (
    read_file_call_arguments,
    split_parallel_task_outputs,
    split_read_files_outputs,
    task_call_arguments,
)
from runtime.token_estimator import (
    emergency_trim,
    estimate_tokens,
    output_tokens_for_call,
    safe_context_limit,
)
from runtime.ports import (
    ObservabilityPort,
    ToolExecutorPort,
    ToolPort,
)
from runtime.trace.events import (
    CONTEXT_BUILD_COMPLETED,
    CONTEXT_BUILD_STARTED,
    CONTEXT_SANITIZED,
    MODEL_ROUTE_ATTEMPTS,
    MODEL_CALL_COMPLETED,
    MODEL_CALL_FAILED,
    MODEL_CALL_STARTED,
    REASONING_STEP_COMPLETED,
    REASONING_STEP_STARTED,
    TOOL_CALL_COMPLETED,
    TOOL_CALL_FAILED,
    TOOL_CALL_STARTED,
)
from runtime.trace.trace_store import event_preview
from runtime.trace.context_metrics import context_build_metrics_from_report
from tools.executor import ToolExecutionRequest, ToolExecutionResult


DEFAULT_MAX_REASONING_STEPS = 24
MAX_UNAVAILABLE_TOOL_ATTEMPTS = 2


@dataclass
class ToolExecutionSummary:
    manual_compact: bool = False
    unavailable_tools: list[str] = field(default_factory=list)
    loop_guard_denied: bool = False
    tool_results: list[dict] = field(default_factory=list)


class ReasoningLoop:
    """Run the model/tool reasoning loop for one agent turn.

    Runtime owns turn setup, context policies, and memory lifecycle. This class
    owns the repeated model call -> tool execution -> model call cycle so other
    agent runtimes can reuse it without copying Runtime internals.
    """

    def __init__(
        self,
        *,
        tools: ToolPort,
        tool_executor: ToolExecutorPort,
        max_tokens: int = 8000,
        max_reasoning_steps: int = DEFAULT_MAX_REASONING_STEPS,
        policies: ExecutionPolicies | None = None,
    ) -> None:
        self.tools = tools
        self.tool_executor = tool_executor
        self.max_tokens = max_tokens
        self.max_reasoning_steps = max(1, int(max_reasoning_steps))
        policies = policies or ExecutionPolicies.minimal(self.max_reasoning_steps)
        self.web_search_policy = policies.web_search
        self.finishing_policy = policies.finishing
        self.working_memory_policy = policies.working_memory
        self.tool_batch_policy = policies.tool_batch

    def run(
        self,
        *,
        session,
        profile,
        build_context: Callable,
        resolve_provider: Callable,
        after_turn: Callable,
        after_tool_calls: Callable | None = None,
        reflection_agent=None,
        on_text: Callable[[str], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        checkpoint_callback: Callable | None = None,
        run_state=None,
        trace_store: ObservabilityPort | None = None,
        trace_parent_span_id: str | None = None,
        run_context=None,
    ) -> None:
        self.run_context = run_context
        reasoning_steps = 0
        unavailable_attempts: dict[str, int] = {}
        empty_model_responses = 0

        while True:
            if self._cancel_requested(cancel_requested):
                self._trace(trace_store, run_state, "user_cancel_requested", {
                    "session_id": session.id,
                    "step": reasoning_steps,
                })
                self._stop_turn(
                    session,
                    self.working_memory_policy.partial_summary(session),
                    reason=StopReason.USER_CANCELLED,
                    profile=profile,
                    after_turn=after_turn,
                    on_text=on_text,
                    run_state=run_state,
                    trace_store=trace_store,
                    reasoning_step=reasoning_steps,
                    checkpoint_callback=checkpoint_callback,
                )
                return

            reasoning_steps += 1
            if self._reasoning_budget_exceeded(session, reasoning_steps):
                self._trace(trace_store, run_state, "reasoning_budget_exceeded", {
                    "attempted_step": reasoning_steps,
                    "max_reasoning_steps": self.max_reasoning_steps,
                })
                self._stop_turn(
                    session,
                    (
                        "本轮已停止：工具推理步骤超过上限 "
                        f"({self.max_reasoning_steps})，已触发循环保护。"
                    ),
                    reason=StopReason.REASONING_STEP_LIMIT,
                    profile=profile,
                    after_turn=after_turn,
                    on_text=on_text,
                    run_state=run_state,
                    trace_store=trace_store,
                    reasoning_step=reasoning_steps,
                    checkpoint_callback=checkpoint_callback,
                )
                return
            if run_state is not None:
                run_state.record_reasoning_step(reasoning_steps)
                if trace_store is not None:
                    trace_store.write_run_state(run_state)
            self._maybe_inject_finishing_reminder(
                session,
                reasoning_steps,
                trace_store=trace_store,
                run_state=run_state,
                run_context=run_context,
            )
            self._checkpoint_reasoning_step(
                session,
                profile,
                step=reasoning_steps,
                phase="started",
                message_count=len(session.messages),
                checkpoint_callback=checkpoint_callback,
            )
            self._trace(trace_store, run_state, "reasoning_step_started", {
                "step": reasoning_steps,
                "session_id": session.id,
                "message_count": len(session.messages),
            })
            self._trace(
                trace_store,
                run_state,
                REASONING_STEP_STARTED,
                {
                    "message_count": len(session.messages),
                },
                step=reasoning_steps,
                span_id=_step_span_id(run_state, reasoning_steps),
                parent_span_id=trace_parent_span_id,
            )
            context_started = time.perf_counter()
            context_span_id = _context_span_id(run_state, reasoning_steps)
            step_span_id = _step_span_id(run_state, reasoning_steps)
            self._trace(
                trace_store,
                run_state,
                CONTEXT_BUILD_STARTED,
                {
                    "message_count_before": len(session.messages),
                },
                step=reasoning_steps,
                span_id=context_span_id,
                parent_span_id=step_span_id,
            )
            turn_context = _call_build_context(
                build_context,
                session,
                profile,
                trace_store=trace_store,
                run_state=run_state,
                trace_parent_span_id=context_span_id,
                reasoning_step=reasoning_steps,
            )
            context_messages = getattr(turn_context, "messages", [])
            context_report = _context_report_payload(
                getattr(turn_context, "report", None)
            )
            context_duration_ms = _elapsed_ms(context_started)
            context_metrics = context_build_metrics_from_report(
                context_report,
                duration_ms=context_duration_ms,
            )
            context_summary = _context_summary(context_messages)
            if context_metrics.get("coding_context_state_enabled"):
                context_summary["coding_context_state"] = context_metrics.get(
                    "coding_context_state",
                    {},
                )
            self._trace(
                trace_store,
                run_state,
                CONTEXT_BUILD_COMPLETED,
                {
                    "duration_ms": context_duration_ms,
                    "message_count_before": len(session.messages),
                    "message_count_after": len(context_messages),
                    "context_summary": context_summary,
                    "context_report": context_report,
                    "context_metrics": context_metrics,
                },
                step=reasoning_steps,
                span_id=context_span_id,
                parent_span_id=step_span_id,
            )

            response = self._reasoning_step(
                session=session,
                context=turn_context,
                profile=profile,
                resolve_provider=resolve_provider,
                on_text=on_text,
                run_state=run_state,
                trace_store=trace_store,
                reasoning_step=reasoning_steps,
                checkpoint_callback=checkpoint_callback,
            )

            if _is_empty_response(response):
                empty_model_responses += 1
                self._checkpoint_reasoning_step(
                    session,
                    profile,
                    step=reasoning_steps,
                    phase="empty_model_response",
                    message_count=len(session.messages),
                    note=f"empty_response_attempt={empty_model_responses}",
                    checkpoint_callback=checkpoint_callback,
                )
                self._trace(
                    trace_store,
                    run_state,
                    REASONING_STEP_COMPLETED,
                    {
                        "reason": StopReason.EMPTY_MODEL_RESPONSE.value,
                        "tool_call_count": 0,
                        "attempt": empty_model_responses,
                    },
                    step=reasoning_steps,
                    span_id=_step_span_id(run_state, reasoning_steps),
                )
                self._trace(trace_store, run_state, "empty_model_response", {
                    "step": reasoning_steps,
                    "attempt": empty_model_responses,
                })
                if empty_model_responses >= 2:
                    self._stop_turn(
                        session,
                        "本轮已停止：模型连续返回空回复且没有工具调用。",
                        reason=StopReason.EMPTY_MODEL_RESPONSE,
                        profile=profile,
                        after_turn=after_turn,
                        on_text=on_text,
                        run_state=run_state,
                        trace_store=trace_store,
                        reasoning_step=reasoning_steps,
                        checkpoint_callback=checkpoint_callback,
                    )
                    return
                session.add_message(
                    "user",
                    (
                        "<runtime-retry reason=\"empty_model_response\">\n"
                        "Your previous response was empty and contained no tool calls. "
                        "Continue the task by either calling an appropriate tool or "
                        "providing a concrete final answer.\n"
                        "</runtime-retry>"
                    ),
                    metadata={
                        "kind": "runtime_retry",
                        "reason": StopReason.EMPTY_MODEL_RESPONSE.value,
                    },
                )
                continue

            empty_model_responses = 0
            self._after_reasoning_step(session, response)
            tool_calls = _response_tool_calls_payload(response)

            if not response.tool_calls:
                self._checkpoint_reasoning_step(
                    session,
                    profile,
                    step=reasoning_steps,
                    phase="assistant_final",
                    message_count=len(session.messages),
                    assistant_summary=response.content or "",
                    checkpoint_callback=checkpoint_callback,
                )
                self._trace(
                    trace_store,
                    run_state,
                    REASONING_STEP_COMPLETED,
                    {
                        "reason": "assistant_final_message",
                        "tool_call_count": 0,
                    },
                    step=reasoning_steps,
                    span_id=_step_span_id(run_state, reasoning_steps),
                )
                self._trace(trace_store, run_state, "reasoning_loop_completed", {
                    "step": reasoning_steps,
                    "reason": "assistant_final_message",
                })
                self.working_memory_policy.complete(
                    session,
                    final_answer=response.content or "",
                    step=reasoning_steps,
                )
                if checkpoint_callback is not None:
                    checkpoint_callback(session)
                after_turn(session)
                return

            execution = self._execute_tool_calls(
                session,
                response,
                profile,
                run_state=run_state,
                trace_store=trace_store,
                reasoning_step=reasoning_steps,
            )
            self._checkpoint_reasoning_step(
                session,
                profile,
                step=reasoning_steps,
                phase="tools_executed",
                message_count=len(session.messages),
                assistant_summary=response.content or "",
                tool_calls=tool_calls,
                tool_results=execution.tool_results,
                checkpoint_callback=checkpoint_callback,
            )
            self._trace(
                trace_store,
                run_state,
                REASONING_STEP_COMPLETED,
                {
                    "reason": "tool_calls_executed",
                    "tool_call_count": len(response.tool_calls),
                    "loop_guard_denied": execution.loop_guard_denied,
                    "manual_compact": execution.manual_compact,
                },
                step=reasoning_steps,
                span_id=_step_span_id(run_state, reasoning_steps),
            )
            if after_tool_calls is not None and after_tool_calls(
                session,
                response,
                execution,
            ):
                self._trace(trace_store, run_state, "reasoning_loop_paused", {
                    "reason": "after_tool_calls_callback",
                })
                after_turn(session)
                return
            if execution.loop_guard_denied:
                if reflection_agent is not None:
                    if self._apply_reflection(
                        reflection_agent,
                        session=session,
                        profile=profile,
                        response=response,
                        execution=execution,
                        reasoning_steps=reasoning_steps,
                        after_turn=after_turn,
                        on_text=on_text,
                        run_state=run_state,
                        trace_store=trace_store,
                        checkpoint_callback=checkpoint_callback,
                        force=True,
                        trigger="loop_guard_denied",
                    ):
                        return
                    self._trace(trace_store, run_state, "loop_guard_reflection_continue", {
                        "step": reasoning_steps,
                    })
                    continue
                self._stop_turn(
                    session,
                    "本轮已停止：模型重复调用同一工具，已触发循环保护。请调整请求后重试。",
                    reason=StopReason.REPEATED_TOOL_CALL,
                    profile=profile,
                    after_turn=after_turn,
                    on_text=on_text,
                    run_state=run_state,
                    trace_store=trace_store,
                    reasoning_step=reasoning_steps,
                    checkpoint_callback=checkpoint_callback,
                )
                return

            for tool_name in execution.unavailable_tools:
                unavailable_attempts[tool_name] = unavailable_attempts.get(tool_name, 0) + 1
                if unavailable_attempts[tool_name] >= MAX_UNAVAILABLE_TOOL_ATTEMPTS:
                    self._stop_turn(
                        session,
                        (
                            "本轮已停止：模型重复请求当前不可用的工具 "
                            f"`{tool_name}`。请切换到允许该工具的模式，"
                            "或让助手使用 `tool_search` 选择当前模式可用的工具。"
                        ),
                        reason=StopReason.UNAVAILABLE_TOOL_LOOP,
                        profile=profile,
                        after_turn=after_turn,
                        on_text=on_text,
                        run_state=run_state,
                        trace_store=trace_store,
                        reasoning_step=reasoning_steps,
                        checkpoint_callback=checkpoint_callback,
                    )
                    return

            if self._apply_reflection(
                reflection_agent,
                session=session,
                profile=profile,
                response=response,
                execution=execution,
                reasoning_steps=reasoning_steps,
                after_turn=after_turn,
                on_text=on_text,
                run_state=run_state,
                trace_store=trace_store,
                checkpoint_callback=checkpoint_callback,
                trigger="scheduled",
            ):
                return

            if execution.manual_compact:
                self._trace(trace_store, run_state, "manual_compact_requested", {
                    "step": reasoning_steps,
                    "handled_by": "context_budget",
                })

    def _reasoning_step(
        self,
        *,
        session,
        context,
        profile,
        resolve_provider: Callable,
        on_text=None,
        run_state=None,
        trace_store=None,
        reasoning_step: int = 0,
        checkpoint_callback: Callable | None = None,
    ):
        provider, model = resolve_provider(session, profile)
        use_stream = supports_streaming(provider, on_text)
        tools = self.tools.schemas_for_turn(session, profile.tool_mode)
        context_messages, dropped_messages = sanitize_context_messages(context.messages)
        context_summary = _context_summary(context_messages, provider=provider)
        if dropped_messages:
            self._trace(
                trace_store,
                run_state,
                CONTEXT_SANITIZED,
                {
                    "dropped_count": len(dropped_messages),
                    "dropped_messages": dropped_messages,
                    "context_summary": context_summary,
                },
                step=reasoning_step,
                span_id=_context_span_id(run_state, reasoning_step),
                parent_span_id=_step_span_id(run_state, reasoning_step),
            )
        safe_limit = safe_context_limit(
            provider,
            reserved_output_tokens=self.max_tokens,
        )
        estimated_tokens = estimate_tokens(context_messages, provider=provider)
        if estimated_tokens > safe_limit:
            before_message_count = len(context_messages)
            before_tokens = estimated_tokens
            context_messages = emergency_trim(
                context_messages,
                max_tokens=safe_limit,
                provider=provider,
            )
            context_summary = _context_summary(context_messages, provider=provider)
            self._trace(
                trace_store,
                run_state,
                "context_emergency_trim",
                {
                    "before_message_count": before_message_count,
                    "after_message_count": len(context_messages),
                    "before_estimated_tokens": before_tokens,
                    "after_estimated_tokens": context_summary["estimated_tokens"],
                    "safe_limit_tokens": safe_limit,
                },
                step=reasoning_step,
                span_id=_context_span_id(run_state, reasoning_step),
                parent_span_id=_step_span_id(run_state, reasoning_step),
            )
        request_max_tokens = output_tokens_for_call(
            provider,
            requested_output_tokens=self.max_tokens,
            input_tokens=context_summary["estimated_tokens"],
        )
        if request_max_tokens != self.max_tokens:
            self._trace(
                trace_store,
                run_state,
                "model_output_tokens_clamped",
                {
                    "configured_max_tokens": self.max_tokens,
                    "request_max_tokens": request_max_tokens,
                    "estimated_input_tokens": context_summary["estimated_tokens"],
                    "safe_input_limit_tokens": safe_limit,
                },
                step=reasoning_step,
                span_id=_context_span_id(run_state, reasoning_step),
                parent_span_id=_step_span_id(run_state, reasoning_step),
            )
        provider_name = type(provider).__name__
        span_id = _model_span_id(run_state, reasoning_step)
        parent_span_id = _step_span_id(run_state, reasoning_step)
        started = time.perf_counter()
        self._trace(trace_store, run_state, "model_requested", {
            "model": model,
            "provider": provider_name,
            "tool_mode": profile.tool_mode,
            "tool_count": len(tools),
            "tool_names": _tool_names(tools),
            "message_count": len(context_messages),
            "max_tokens": request_max_tokens,
            "configured_max_tokens": self.max_tokens,
            "stream": use_stream,
            "context_summary": context_summary,
        })
        self._trace(
            trace_store,
            run_state,
            MODEL_CALL_STARTED,
            {
                "model": model,
                "provider": provider_name,
                "tool_mode": profile.tool_mode,
                "tool_count": len(tools),
                "tool_names": _tool_names(tools),
                "message_count": len(context_messages),
                "max_tokens": request_max_tokens,
                "configured_max_tokens": self.max_tokens,
                "stream": use_stream,
                "context_summary": context_summary,
            },
            step=reasoning_step,
            span_id=span_id,
            parent_span_id=parent_span_id,
        )
        try:
            response = invoke_model(
                provider,
                model=model,
                messages=context_messages,
                tools=tools,
                max_tokens=request_max_tokens,
                on_text=on_text,
            )
        except Exception as exc:
            route_attempts = getattr(exc, "attempts", None)
            if route_attempts:
                self._trace(
                    trace_store,
                    run_state,
                    MODEL_ROUTE_ATTEMPTS,
                    {
                        "purpose": getattr(exc, "purpose", ""),
                        "attempts": route_attempts,
                    },
                    step=reasoning_step,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                )
            self._trace(
                trace_store,
                run_state,
                MODEL_CALL_FAILED,
                {
                    "model": model,
                    "provider": provider_name,
                    "duration_ms": _elapsed_ms(started),
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "route_attempts": route_attempts or [],
                    "context_summary": context_summary,
                },
                step=reasoning_step,
                span_id=span_id,
                parent_span_id=parent_span_id,
            )
            self._checkpoint_reasoning_step(
                session,
                profile,
                step=reasoning_step,
                phase="model_error",
                message_count=len(session.messages),
                note=f"{type(exc).__name__}: {exc}",
                checkpoint_callback=checkpoint_callback,
            )
            raise
        if on_text is not None and not use_stream and response.content:
            on_text(response.content)
        completed_payload = {
            "model": model,
            "provider": provider_name,
            "duration_ms": _elapsed_ms(started),
            "content_preview": event_preview(response.content),
            "tool_call_count": len(response.tool_calls),
            "tool_calls": [
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments_preview": event_preview(call.arguments),
                }
                for call in response.tool_calls
            ],
            "usage": _usage_payload(getattr(response, "usage", None)),
            "provider_metadata": getattr(response, "provider_metadata", {}) or {},
        }
        self._trace(
            trace_store,
            run_state,
            MODEL_CALL_COMPLETED,
            completed_payload,
            step=reasoning_step,
            span_id=span_id,
            parent_span_id=parent_span_id,
        )
        self._trace(trace_store, run_state, "model_returned", {
            "content_preview": event_preview(response.content),
            "tool_calls": [
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
                for call in response.tool_calls
            ],
            "tool_call_count": len(response.tool_calls),
        })
        return response

    def _checkpoint_reasoning_step(
        self,
        session,
        profile,
        *,
        step: int,
        phase: str,
        message_count: int | None = None,
        assistant_summary: str = "",
        tool_calls: list[dict] | None = None,
        tool_results: list[dict] | None = None,
        note: str = "",
        checkpoint_callback: Callable | None = None,
    ) -> None:
        self.working_memory_policy.checkpoint(
            session,
            profile,
            step=step,
            phase=phase,
            message_count=message_count,
            assistant_summary=assistant_summary,
            tool_calls=tool_calls,
            tool_results=tool_results,
            note=note,
            checkpoint_callback=checkpoint_callback,
        )

    def _after_reasoning_step(self, session, response) -> None:
        if response.raw_message:
            session.messages.append(response.raw_message)
        elif response.content is not None or response.tool_calls:
            session.messages.append({
                "role": "assistant",
                "content": response.content or "",
            })

    def _execute_tool_calls(
        self,
        session,
        response,
        profile,
        *,
        run_state=None,
        trace_store=None,
        reasoning_step: int = 0,
    ) -> ToolExecutionSummary:
        tool_calls = list(response.tool_calls or [])
        if self._should_auto_parallelize_task_calls(
            tool_calls,
            session=session,
            mode=profile.tool_mode,
        ):
            return self._execute_auto_parallelized_task_calls(
                session,
                tool_calls,
                profile,
                run_state=run_state,
                trace_store=trace_store,
                reasoning_step=reasoning_step,
            )
        if self._should_auto_batch_read_file_calls(
            tool_calls,
            session=session,
            mode=profile.tool_mode,
        ):
            return self._execute_auto_batched_read_file_calls(
                session,
                tool_calls,
                profile,
                run_state=run_state,
                trace_store=trace_store,
                reasoning_step=reasoning_step,
            )

        summary = ToolExecutionSummary()

        for call in tool_calls:
            span_id = _tool_span_id(run_state, reasoning_step, call.id)
            parent_span_id = _step_span_id(run_state, reasoning_step)
            self._trace(
                trace_store,
                run_state,
                TOOL_CALL_STARTED,
                {
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "arguments_preview": event_preview(call.arguments),
                    "source": str((session.metadata or {}).get("kind", "passive")),
                },
                step=reasoning_step,
                span_id=span_id,
                parent_span_id=parent_span_id,
            )
            if call.name == "compact":
                summary.manual_compact = True
                started = time.perf_counter()
                output = "Manual compact requested."
                if run_state is not None:
                    run_state.record_tool(call.name)
                    if trace_store is not None:
                        trace_store.write_run_state(run_state)
                duration_ms = _elapsed_ms(started)
                summary.tool_results.append({
                    "name": call.name,
                    "output": output,
                    "status": "success",
                    "final_arguments": call.arguments,
                })
                session.messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": output,
                    "status": "success",
                    "final_arguments": call.arguments,
                    "pre_hook_trace": [],
                    "post_hook_trace": [],
                })
                self._trace(
                    trace_store,
                    run_state,
                    TOOL_CALL_COMPLETED,
                    {
                        "tool_call_id": call.id,
                        "tool_name": call.name,
                        "status": "success",
                        "duration_ms": duration_ms,
                        "final_arguments_preview": event_preview(call.arguments),
                        "output_preview": output,
                        "pre_hook_trace": [],
                        "post_hook_trace": [],
                    },
                    step=reasoning_step,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                )
                self._trace(trace_store, run_state, "tool_executed", {
                    "call_id": call.id,
                    "name": call.name,
                    "status": "success",
                    "final_arguments": call.arguments,
                    "output_preview": output,
                    "pre_hook_trace": [],
                    "post_hook_trace": [],
                })
            else:
                execution_error = self._tool_execution_error(
                    call.name,
                    session=session,
                    mode=profile.tool_mode,
                )
                search_budget_denial = self._web_search_budget_denial(
                    session,
                    call.name,
                )
                request = ToolExecutionRequest(
                    call_id=call.id,
                    tool_name=call.name,
                    arguments=call.arguments,
                    session_id=session.id,
                    source=str((session.metadata or {}).get("kind", "passive")),
                    metadata=session.metadata,
                )
                if search_budget_denial:
                    started = time.perf_counter()
                    result = self._denied_tool_result(
                        output=search_budget_denial,
                        arguments=call.arguments,
                        duration_ms=_elapsed_ms(started),
                    )
                else:
                    result = self.tool_executor.execute(
                        request,
                        lambda name, args: self.tools.execute(
                            name,
                            args,
                            session=session,
                            mode=profile.tool_mode,
                            trace_store=trace_store,
                            run_state=run_state,
                            parent_span_id=span_id,
                        ),
                    )
                output = self._with_web_search_budget_notice(
                    session,
                    call.name,
                    result.output,
                )
                if execution_error:
                    summary.unavailable_tools.append(call.name)
                if _is_loop_guard_denial(result):
                    summary.loop_guard_denied = True
                summary.tool_results.append({
                    "name": call.name,
                    "output": output,
                    "status": result.status,
                    "final_arguments": result.final_arguments,
                })
                if run_state is not None:
                    run_state.record_tool(call.name)
                    if trace_store is not None:
                        trace_store.write_run_state(run_state)
                tool_payload = {
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "status": result.status,
                    "duration_ms": result.duration_ms,
                    "execution_error": execution_error,
                    "final_arguments_preview": event_preview(result.final_arguments),
                    "output_preview": event_preview(output),
                    "error_type": result.error_type,
                    "error_message": result.error_message,
                    "metadata": dict(result.metadata or {}),
                    "truncated_output": _tool_output_truncated(output),
                    "subagent_incomplete_count": _subagent_incomplete_count(
                        call.name,
                        output,
                    ),
                    "pre_hook_trace": [
                        item.__dict__ for item in result.pre_hook_trace
                    ],
                    "post_hook_trace": [
                        item.__dict__ for item in result.post_hook_trace
                    ],
                }
                self._trace(
                    trace_store,
                    run_state,
                    (
                        TOOL_CALL_FAILED
                        if result.status == "error"
                        else TOOL_CALL_COMPLETED
                    ),
                    tool_payload,
                    step=reasoning_step,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                )
                self._trace(trace_store, run_state, "tool_executed", {
                    "call_id": call.id,
                    "name": call.name,
                    "status": result.status,
                    "execution_error": execution_error,
                    "final_arguments": result.final_arguments,
                    "output_preview": event_preview(output),
                    "metadata": dict(result.metadata or {}),
                    "pre_hook_trace": [
                        item.__dict__ for item in result.pre_hook_trace
                    ],
                    "post_hook_trace": [
                        item.__dict__ for item in result.post_hook_trace
                    ],
                })

                session.messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": output,
                    "status": result.status,
                    "final_arguments": result.final_arguments,
                    "metadata": dict(result.metadata or {}),
                    "pre_hook_trace": [
                        item.__dict__ for item in result.pre_hook_trace
                    ],
                    "post_hook_trace": [
                        item.__dict__ for item in result.post_hook_trace
                    ],
                })
        return summary

    def _should_auto_parallelize_task_calls(
        self,
        tool_calls: list,
        *,
        session,
        mode: str,
    ) -> bool:
        return self.tool_batch_policy.should_parallelize_tasks(
            tool_calls,
            available=not self._tool_execution_error(
                "parallel_tasks",
                session=session,
                mode=mode,
            ),
        )

    def _should_auto_batch_read_file_calls(
        self,
        tool_calls: list,
        *,
        session,
        mode: str,
    ) -> bool:
        return self.tool_batch_policy.should_batch_reads(
            tool_calls,
            available=not self._tool_execution_error(
                "read_files",
                session=session,
                mode=mode,
            ),
        )

    def _execute_auto_parallelized_task_calls(
        self,
        session,
        tool_calls: list,
        profile,
        *,
        run_state=None,
        trace_store=None,
        reasoning_step: int = 0,
    ) -> ToolExecutionSummary:
        summary = ToolExecutionSummary()
        parent_span_id = _step_span_id(run_state, reasoning_step)
        source = str((session.metadata or {}).get("kind", "passive"))
        call_contexts = []
        for call in tool_calls:
            span_id = _tool_span_id(run_state, reasoning_step, call.id)
            call_contexts.append((call, span_id))
            self._trace(
                trace_store,
                run_state,
                TOOL_CALL_STARTED,
                {
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "arguments_preview": event_preview(call.arguments),
                    "source": source,
                    "auto_parallelized_task_batch": True,
                    "parallel_task_count": len(tool_calls),
                },
                step=reasoning_step,
                span_id=span_id,
                parent_span_id=parent_span_id,
            )

        tasks = [task_call_arguments(call) for call in tool_calls]
        parallel_arguments = {
            "tasks": tasks,
            "max_workers": len(tasks),
        }
        request = ToolExecutionRequest(
            call_id=f"auto_parallel:{getattr(tool_calls[0], 'id', 'task')}",
            tool_name="parallel_tasks",
            arguments=parallel_arguments,
            session_id=session.id,
            source=source,
            metadata={
                **dict(session.metadata or {}),
                "auto_parallelized_task_batch": True,
            },
        )
        result = self.tool_executor.execute(
            request,
            lambda name, args: self.tools.execute(
                name,
                args,
                session=session,
                mode=profile.tool_mode,
                trace_store=trace_store,
                run_state=run_state,
                parent_span_id=parent_span_id,
            ),
        )
        outputs = split_parallel_task_outputs(result.output, len(tool_calls))
        hook_metadata = {
            **dict(result.metadata or {}),
            "auto_parallelized_task_batch": True,
            "parallel_task_count": len(tool_calls),
        }
        if _is_loop_guard_denial(result):
            summary.loop_guard_denied = True

        for index, (call, span_id) in enumerate(call_contexts):
            output = outputs[index]
            summary.tool_results.append({
                "name": call.name,
                "output": output,
                "status": result.status,
                "final_arguments": call.arguments,
            })
            if run_state is not None:
                run_state.record_tool(call.name)
                if trace_store is not None:
                    trace_store.write_run_state(run_state)
            tool_payload = {
                "tool_call_id": call.id,
                "tool_name": call.name,
                "status": result.status,
                "duration_ms": result.duration_ms,
                "execution_error": None,
                "final_arguments_preview": event_preview(call.arguments),
                "output_preview": event_preview(output),
                "error_type": result.error_type,
                "error_message": result.error_message,
                "metadata": {
                    **hook_metadata,
                    "parallel_batch_index": index,
                },
                "truncated_output": _tool_output_truncated(output),
                "subagent_incomplete_count": _subagent_incomplete_count(
                    call.name,
                    output,
                ),
                "pre_hook_trace": [
                    item.__dict__ for item in result.pre_hook_trace
                ],
                "post_hook_trace": [
                    item.__dict__ for item in result.post_hook_trace
                ],
            }
            self._trace(
                trace_store,
                run_state,
                (
                    TOOL_CALL_FAILED
                    if result.status == "error"
                    else TOOL_CALL_COMPLETED
                ),
                tool_payload,
                step=reasoning_step,
                span_id=span_id,
                parent_span_id=parent_span_id,
            )
            self._trace(trace_store, run_state, "tool_executed", {
                "call_id": call.id,
                "name": call.name,
                "status": result.status,
                "execution_error": None,
                "final_arguments": call.arguments,
                "output_preview": event_preview(output),
                "metadata": {
                    **hook_metadata,
                    "parallel_batch_index": index,
                },
                "pre_hook_trace": [
                    item.__dict__ for item in result.pre_hook_trace
                ],
                "post_hook_trace": [
                    item.__dict__ for item in result.post_hook_trace
                ],
            })
            session.messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": output,
                "status": result.status,
                "final_arguments": call.arguments,
                "metadata": {
                    **hook_metadata,
                    "parallel_batch_index": index,
                },
                "pre_hook_trace": [
                    item.__dict__ for item in result.pre_hook_trace
                ],
                "post_hook_trace": [
                    item.__dict__ for item in result.post_hook_trace
                ],
            })
        return summary

    def _execute_auto_batched_read_file_calls(
        self,
        session,
        tool_calls: list,
        profile,
        *,
        run_state=None,
        trace_store=None,
        reasoning_step: int = 0,
    ) -> ToolExecutionSummary:
        summary = ToolExecutionSummary()
        parent_span_id = _step_span_id(run_state, reasoning_step)
        source = str((session.metadata or {}).get("kind", "passive"))
        call_contexts = []
        for call in tool_calls:
            span_id = _tool_span_id(run_state, reasoning_step, call.id)
            call_contexts.append((call, span_id))
            self._trace(
                trace_store,
                run_state,
                TOOL_CALL_STARTED,
                {
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "arguments_preview": event_preview(call.arguments),
                    "source": source,
                    "auto_batched_read_file": True,
                    "batch_read_count": len(tool_calls),
                },
                step=reasoning_step,
                span_id=span_id,
                parent_span_id=parent_span_id,
            )

        batch_arguments = {
            "files": [read_file_call_arguments(call) for call in tool_calls],
            "format": "json",
        }
        request = ToolExecutionRequest(
            call_id=f"auto_read_files:{getattr(tool_calls[0], 'id', 'read_file')}",
            tool_name="read_files",
            arguments=batch_arguments,
            session_id=session.id,
            source=source,
            metadata={
                **dict(session.metadata or {}),
                "auto_batched_read_file": True,
            },
        )
        result = self.tool_executor.execute(
            request,
            lambda name, args: self.tools.execute(
                name,
                args,
                session=session,
                mode=profile.tool_mode,
                trace_store=trace_store,
                run_state=run_state,
                parent_span_id=parent_span_id,
            ),
        )
        outputs = split_read_files_outputs(result.output, len(tool_calls))
        hook_metadata = {
            **dict(result.metadata or {}),
            "auto_batched_read_file": True,
            "batch_read_count": len(tool_calls),
        }
        if _is_loop_guard_denial(result):
            summary.loop_guard_denied = True

        for index, (call, span_id) in enumerate(call_contexts):
            output = outputs[index]
            summary.tool_results.append({
                "name": call.name,
                "output": output,
                "status": result.status,
                "final_arguments": call.arguments,
            })
            if run_state is not None:
                run_state.record_tool(call.name)
                if trace_store is not None:
                    trace_store.write_run_state(run_state)
            tool_payload = {
                "tool_call_id": call.id,
                "tool_name": call.name,
                "status": result.status,
                "duration_ms": result.duration_ms,
                "execution_error": None,
                "final_arguments_preview": event_preview(call.arguments),
                "output_preview": event_preview(output),
                "error_type": result.error_type,
                "error_message": result.error_message,
                "metadata": {
                    **hook_metadata,
                    "batch_read_index": index,
                },
                "truncated_output": _tool_output_truncated(output),
                "subagent_incomplete_count": _subagent_incomplete_count(
                    call.name,
                    output,
                ),
                "pre_hook_trace": [
                    item.__dict__ for item in result.pre_hook_trace
                ],
                "post_hook_trace": [
                    item.__dict__ for item in result.post_hook_trace
                ],
            }
            self._trace(
                trace_store,
                run_state,
                (
                    TOOL_CALL_FAILED
                    if result.status == "error"
                    else TOOL_CALL_COMPLETED
                ),
                tool_payload,
                step=reasoning_step,
                span_id=span_id,
                parent_span_id=parent_span_id,
            )
            self._trace(trace_store, run_state, "tool_executed", {
                "call_id": call.id,
                "name": call.name,
                "status": result.status,
                "execution_error": None,
                "final_arguments": call.arguments,
                "output_preview": event_preview(output),
                "metadata": {
                    **hook_metadata,
                    "batch_read_index": index,
                },
                "pre_hook_trace": [
                    item.__dict__ for item in result.pre_hook_trace
                ],
                "post_hook_trace": [
                    item.__dict__ for item in result.post_hook_trace
                ],
            })
            session.messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": output,
                "status": result.status,
                "final_arguments": call.arguments,
                "metadata": {
                    **hook_metadata,
                    "batch_read_index": index,
                },
                "pre_hook_trace": [
                    item.__dict__ for item in result.pre_hook_trace
                ],
                "post_hook_trace": [
                    item.__dict__ for item in result.post_hook_trace
                ],
            })
        return summary

    def _tool_execution_error(self, name: str, *, session, mode: str) -> str | None:
        checker = getattr(self.tools, "execution_error_for_turn", None)
        if checker is None:
            return None
        return checker(name, session=session, mode=mode)

    def _reasoning_budget_exceeded(self, session, reasoning_steps: int) -> bool:
        return reasoning_steps > self.max_reasoning_steps

    def _cancel_requested(self, cancel_requested: Callable[[], bool] | None) -> bool:
        if cancel_requested is None:
            return False
        try:
            return bool(cancel_requested())
        except Exception:
            return False

    def _maybe_inject_finishing_reminder(
        self,
        session,
        reasoning_steps: int,
        *,
        trace_store=None,
        run_state=None,
        run_context=None,
    ) -> None:
        self.finishing_policy.inject(
            session,
            reasoning_steps,
            state=getattr(run_context, "state", None),
            trace=lambda event, payload: self._trace(
                trace_store,
                run_state,
                event,
                payload,
                step=reasoning_steps,
                span_id=_step_span_id(run_state, reasoning_steps),
            ),
        )

    def _finishing_reminder_step(self) -> int:
        return self.finishing_policy._reminder_step()

    def _web_search_budget_denial(self, session, tool_name: str) -> str:
        return self.web_search_policy.denial(
            session,
            tool_name,
            state=getattr(getattr(self, "run_context", None), "state", None),
        )

    def _with_web_search_budget_notice(
        self,
        session,
        tool_name: str,
        output: str,
    ) -> str:
        return self.web_search_policy.add_notice(
            session,
            tool_name,
            output,
            state=getattr(getattr(self, "run_context", None), "state", None),
        )

    def _denied_tool_result(self, *, output: str, arguments: dict, duration_ms: float):
        return ToolExecutionResult(
            status="denied",
            output=output,
            final_arguments=arguments,
            duration_ms=duration_ms,
            error_type="ToolDenied",
            error_message=output,
        )

    def _stop_turn(
        self,
        session,
        message: str,
        *,
        reason: str | StopReason,
        profile=None,
        after_turn: Callable,
        on_text: Callable[[str], None] | None,
        run_state=None,
        trace_store=None,
        reasoning_step: int | None = None,
        checkpoint_callback: Callable | None = None,
    ) -> None:
        reason_value = str(reason)
        session.add_message(
            "assistant",
            message,
            metadata={
                "kind": "agent_loop_guard",
                "reason": reason_value,
            },
        )
        session.metadata[REASONING_LOOP_STOP_REASON_KEY] = reason_value
        session.metadata[REASONING_LOOP_STOP_MESSAGE_KEY] = message
        state = getattr(getattr(self, "run_context", None), "state", None)
        if state is not None:
            state.stop_reason = reason_value
            state.stop_message = message
        checkpoint_step = reasoning_step
        if checkpoint_step is None and run_state is not None:
            checkpoint_step = getattr(run_state, "reasoning_steps", 0)
        self.working_memory_policy.stop(
            session,
            profile,
            reason=reason_value,
            message=message,
            step=checkpoint_step,
            checkpoint_callback=checkpoint_callback,
        )
        if on_text is not None:
            on_text(message)
        if run_state is not None:
            run_state.stop(reason_value, message)
            if trace_store is not None:
                trace_store.write_run_state(run_state)
        self._trace(trace_store, run_state, "run_stopped", {
            "reason": reason_value,
            "message_preview": event_preview(message),
        })
        after_turn(session)

    def _apply_reflection(
        self,
        reflection_agent,
        *,
        session,
        profile,
        response,
        execution: ToolExecutionSummary,
        reasoning_steps: int,
        after_turn: Callable,
        on_text: Callable[[str], None] | None,
        run_state=None,
        trace_store=None,
        checkpoint_callback: Callable | None = None,
        force: bool = False,
        trigger: str = "scheduled",
    ) -> bool:
        if reflection_agent is None:
            return False

        should_reflect = getattr(reflection_agent, "should_reflect", None)
        if not force and should_reflect is not None and not should_reflect(
            session=session,
            profile=profile,
            response=response,
            execution=execution,
            reasoning_steps=reasoning_steps,
        ):
            return False

        decision = reflection_agent.reflect(
            session=session,
            profile=profile,
            response=response,
            execution=execution,
            reasoning_steps=reasoning_steps,
        )
        action = str(getattr(decision, "action", "continue") or "continue").lower()
        instruction = str(getattr(decision, "instruction", "") or "").strip()
        message = str(getattr(decision, "message", "") or "").strip()
        reason = str(getattr(decision, "reason", "") or "").strip()
        self._trace(trace_store, run_state, "reflection_decision", {
            "action": action,
            "trigger": trigger,
            "forced": force,
            "reason": reason,
            "message_preview": event_preview(message),
            "instruction_preview": event_preview(instruction),
        })

        if action in {"stop", "ask_user"}:
            self._stop_turn(
                session,
                message or reason or "本轮已停止：reflection agent 建议暂停当前流程。",
                reason=f"reflection_{action}",
                profile=profile,
                after_turn=after_turn,
                on_text=on_text,
                run_state=run_state,
                trace_store=trace_store,
                checkpoint_callback=checkpoint_callback,
            )
            return True

        if instruction:
            session.messages.append({
                "role": "user",
                "content": (
                    f"<reflection-instruction critical=\"true\" action=\"{action}\" reason=\"{_xml_escape(reason)}\">\n"
                    f"{instruction}\n"
                    "</reflection-instruction>"
                ),
                "metadata": {
                    "kind": "reflection_instruction",
                    "critical": True,
                    "action": action,
                    "trigger": trigger,
                    "reason": reason,
                },
            })
            self._trace(trace_store, run_state, "reflection_revise", {
                "action": action,
                "trigger": trigger,
                "reason": reason,
                "instruction_preview": event_preview(instruction),
            })
        return False

    def _trace(
        self,
        trace_store,
        run_state,
        event_name: str,
        payload: dict,
        *,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        step: int | None = None,
    ) -> None:
        if trace_store is not None and run_state is not None:
            trace_store.append_event(
                run_state,
                event_name,
                payload,
                span_id=span_id,
                parent_span_id=parent_span_id,
                step=step,
            )


def _usage_payload(usage) -> dict:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return {
            "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens"),
            "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
    try:
        return asdict(usage)
    except TypeError:
        return {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _context_summary(messages: list[dict], *, provider=None) -> dict:
    by_role: dict[str, int] = {}
    empty_assistant_messages = 0
    tool_result_messages = 0
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown")
        by_role[role] = by_role.get(role, 0) + 1
        if role == "assistant" and is_empty_assistant_message(message):
            empty_assistant_messages += 1
        if role == "tool":
            tool_result_messages += 1
    return {
        "message_count": len(messages or []),
        "roles": by_role,
        "assistant_messages": by_role.get("assistant", 0),
        "user_messages": by_role.get("user", 0),
        "tool_messages": tool_result_messages,
        "empty_assistant_messages": empty_assistant_messages,
        "estimated_tokens": estimate_tokens(messages or [], provider=provider),
    }


def _context_report_payload(report) -> dict:
    if report is None:
        return {}
    if hasattr(report, "to_dict"):
        return report.to_dict()
    if isinstance(report, dict):
        return dict(report)
    return getattr(report, "__dict__", {}) or {}


def _is_loop_guard_denial(result: ToolExecutionResult) -> bool:
    traces = list(result.pre_hook_trace or []) + list(result.post_hook_trace or [])
    return any(
        item.hook_name == "tool_loop_guard" and item.decision == "deny"
        for item in traces
    )


def _tool_output_truncated(output: str) -> bool:
    text = str(output or "")
    return any(
        marker in text
        for marker in (
            "To continue: read_file(",
            "To continue: storage_read_file(",
            "To continue: sandbox_read_file(",
            "To continue: list_files(",
            "To continue: repo_map(",
            "...[line truncated]",
            "...[truncated]",
        )
    )


def _subagent_incomplete_count(tool_name: str, output: str) -> int:
    if tool_name != "parallel_tasks":
        return 0
    text = str(output or "")
    return max(
        text.count('"truncated": true'),
        text.count(INCOMPLETE_STEP_LIMIT_PREFIX),
        text.count('"incomplete": true'),
    )


def _xml_escape(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _is_empty_response(response) -> bool:
    if getattr(response, "tool_calls", None):
        return False
    content = getattr(response, "content", None)
    if content is None:
        return True
    if isinstance(content, str):
        return content.strip() == ""
    if isinstance(content, list):
        return len(content) == 0
    return False


def _response_tool_calls_payload(response) -> list[dict]:
    payload = []
    for call in getattr(response, "tool_calls", []) or []:
        payload.append({
            "id": getattr(call, "id", ""),
            "name": getattr(call, "name", ""),
            "arguments": getattr(call, "arguments", {}),
        })
    return payload


def _tool_names(tools: list[dict]) -> list[str]:
    names = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict):
            name = str(function.get("name") or "").strip()
        else:
            name = str(tool.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _context_span_id(run_state, step: int) -> str:
    return f"{_span_prefix(run_state)}:context:{step}"


def _step_span_id(run_state, step: int) -> str:
    return f"{_span_prefix(run_state)}:step:{step}"


def _model_span_id(run_state, step: int) -> str:
    return f"{_span_prefix(run_state)}:model:{step}"


def _tool_span_id(run_state, step: int, call_id: str) -> str:
    suffix = str(call_id or "unknown").replace(":", "_")
    return f"{_span_prefix(run_state)}:tool:{step}:{suffix}"


def _span_prefix(run_state) -> str:
    metadata = getattr(run_state, "metadata", {}) or {}
    prefix = metadata.get("trace_span_prefix") if isinstance(metadata, dict) else None
    return str(prefix or getattr(run_state, "run_id", "run") or "run")


def _call_build_context(
    build_context,
    session,
    profile,
    **kwargs,
):
    call_kwargs = {
        name: value
        for name, value in kwargs.items()
        if _accepts_keyword(build_context, name)
    }
    return build_context(session, profile, **call_kwargs)


def _accepts_keyword(fn, name: str) -> bool:
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return name in signature.parameters


def _int_metadata(session, key: str, default: int) -> int:
    try:
        return int((getattr(session, "metadata", {}) or {}).get(key, default))
    except (TypeError, ValueError):
        return int(default)
