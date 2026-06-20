import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from config import WORKDIR


@dataclass
class ToolExecutionRequest:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    session_id: str = ""
    source: str = "passive"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookOutcome:
    deny_reason: str | None = None
    updated_arguments: dict[str, Any] | None = None


@dataclass
class HookTraceItem:
    hook_name: str
    matched: bool
    decision: str = "allow"
    reason: str = ""


@dataclass
class ToolExecutionResult:
    status: str
    output: str
    final_arguments: dict[str, Any]
    duration_ms: float | None = None
    error_type: str | None = None
    error_message: str | None = None
    pre_hook_trace: list[HookTraceItem] = field(default_factory=list)
    post_hook_trace: list[HookTraceItem] = field(default_factory=list)


class ToolHook:
    name: str = "hook"

    def matches(self, request: ToolExecutionRequest) -> bool:
        return False

    def before(self, request: ToolExecutionRequest) -> HookOutcome:
        return HookOutcome()

    def after(
        self,
        request: ToolExecutionRequest,
        result: ToolExecutionResult,
    ) -> HookOutcome | None:
        return None

    def reset_turn(self, session_id: str) -> None:
        return None


class ToolExecutor:
    def __init__(self, hooks: list[ToolHook] | None = None) -> None:
        self.hooks = hooks or []

    def execute(self, request: ToolExecutionRequest, invoker: Callable[[str, dict], str]) -> ToolExecutionResult:
        started = time.perf_counter()
        arguments = dict(request.arguments)
        pre_traces = []

        for hook in self.hooks:
            matched = hook.matches(request)
            if not matched:
                pre_traces.append(HookTraceItem(
                    hook_name=hook.name,
                    matched=False,
                ))
                continue

            outcome = hook.before(request)
            if outcome.updated_arguments is not None:
                arguments = dict(outcome.updated_arguments)

            if outcome.deny_reason:
                pre_traces.append(HookTraceItem(
                    hook_name=hook.name,
                    matched=True,
                    decision="deny",
                    reason=outcome.deny_reason,
                ))
                result = ToolExecutionResult(
                    status="denied",
                    output=outcome.deny_reason,
                    final_arguments=arguments,
                    duration_ms=_elapsed_ms(started),
                    error_type="ToolDenied",
                    error_message=outcome.deny_reason,
                    pre_hook_trace=pre_traces,
                )
                self._run_after_hooks(request, result)
                return result

            pre_traces.append(HookTraceItem(
                hook_name=hook.name,
                matched=True,
                decision="allow",
            ))

        try:
            output = invoker(request.tool_name, arguments)
            result = ToolExecutionResult(
                status="success",
                output=output,
                final_arguments=arguments,
                duration_ms=_elapsed_ms(started),
                pre_hook_trace=pre_traces,
            )
        except Exception as e:
            result = ToolExecutionResult(
                status="error",
                output=f"Tool error: {e}",
                final_arguments=arguments,
                duration_ms=_elapsed_ms(started),
                error_type=type(e).__name__,
                error_message=str(e),
                pre_hook_trace=pre_traces,
            )
        self._run_after_hooks(request, result)
        return result

    def reset_turn(self, session_id: str) -> None:
        for hook in self.hooks:
            hook.reset_turn(session_id)

    def _run_after_hooks(self, request: ToolExecutionRequest, result: ToolExecutionResult) -> None:
        for hook in self.hooks:
            matched = hook.matches(request)
            if not matched:
                result.post_hook_trace.append(HookTraceItem(
                    hook_name=hook.name,
                    matched=False,
                ))
                continue
            try:
                outcome = hook.after(request, result)
                if isinstance(outcome, HookOutcome):
                    if outcome.updated_arguments is not None:
                        result.final_arguments = dict(outcome.updated_arguments)
                    if outcome.deny_reason:
                        result.status = "denied"
                        result.output = outcome.deny_reason
                        result.error_type = "ToolDenied"
                        result.error_message = outcome.deny_reason
                        result.post_hook_trace.append(HookTraceItem(
                            hook_name=hook.name,
                            matched=True,
                            decision="deny",
                            reason=outcome.deny_reason,
                        ))
                        continue
                result.post_hook_trace.append(HookTraceItem(
                    hook_name=hook.name,
                    matched=True,
                    decision="record",
                ))
            except Exception as e:
                result.post_hook_trace.append(HookTraceItem(
                    hook_name=hook.name,
                    matched=True,
                    decision="error",
                    reason=str(e),
                ))


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
