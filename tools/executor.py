import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolExecutionRequest:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    session_id: str = ""
    source: str = "passive"
    metadata: dict[str, Any] = field(default_factory=dict)
    session: Any = None


@dataclass
class HookOutcome:
    deny_reason: str | None = None
    updated_arguments: dict[str, Any] | None = None
    updated_output: str | None = None


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
    metadata: dict[str, Any] = field(default_factory=dict)
    pre_hook_trace: list[HookTraceItem] = field(default_factory=list)
    post_hook_trace: list[HookTraceItem] = field(default_factory=list)
    exit_code: int | None = None
    effect_state: str = "unknown"

    def __post_init__(self) -> None:
        self.sync_metadata()

    def sync_metadata(self) -> None:
        # Keep older trace/context consumers on the same execution facts.
        self.metadata.update({
            "status": self.status,
            "exit_code": self.exit_code,
            "effect_state": self.effect_state,
            "error_type": self.error_type,
            "error_message": self.error_message,
        })


class ToolHandlerOutput(str):
    """Legacy string view with explicit host-produced execution facts.

    A successful handler return says nothing about task verification. Unknown
    effects stay unknown, including for shell exit zero.
    """

    def __new__(
        cls, output: str, *, metadata: dict[str, Any] | None = None,
        status: str = "success", error_type: str | None = None,
        error_message: str | None = None, exit_code: int | None = None,
        effect_state: str = "unknown",
    ):
        if status not in {"success", "error", "denied", "timeout", "cancelled", "unknown"}:
            raise ValueError(f"Invalid tool status: {status}")
        if effect_state not in {"not_started", "no_effect", "applied", "partial", "unknown"}:
            raise ValueError(f"Invalid effect state: {effect_state}")
        instance = super().__new__(cls, str(output))
        instance.status = status
        instance.error_type = error_type
        instance.error_message = error_message
        instance.exit_code = exit_code
        instance.effect_state = effect_state
        instance.metadata = dict(metadata or {})
        instance.metadata.update({
            "status": status, "error_type": error_type,
            "error_message": error_message, "exit_code": exit_code,
            "effect_state": effect_state,
        })
        return instance


def tool_error(message: str, *, error_type: str = "ToolError",
               status: str = "error", effect_state: str = "unknown",
               exit_code: int | None = None) -> ToolHandlerOutput:
    return ToolHandlerOutput(
        message, status=status, error_type=error_type, error_message=message,
        effect_state=effect_state, exit_code=exit_code,
    )


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
        if not isinstance(request.arguments, dict):
            return ToolExecutionResult(
                status="error", output="Error: Tool arguments must be an object",
                final_arguments={}, error_type="InvalidToolArguments",
                error_message="Tool arguments must be an object", effect_state="not_started",
                duration_ms=_elapsed_ms(started),
            )
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
                    effect_state="not_started",
                    pre_hook_trace=pre_traces,
                )
                self._run_after_hooks(request, result)
                return result

            if outcome.updated_output is not None:
                pre_traces.append(HookTraceItem(
                    hook_name=hook.name,
                    matched=True,
                    decision="cache",
                ))
                result = ToolExecutionResult(
                    status="success",
                    output=str(outcome.updated_output),
                    metadata=dict(getattr(outcome.updated_output, "metadata", {}) or {}),
                    effect_state="no_effect",
                    final_arguments=arguments,
                    duration_ms=_elapsed_ms(started),
                    pre_hook_trace=pre_traces,
                )
                self._run_after_hooks(request, result)
                return result

            pre_traces.append(HookTraceItem(
                hook_name=hook.name,
                matched=True,
                decision="allow",
            ))

        invoked = False
        try:
            from runtime.execution.budget import current_budget

            budget = current_budget()
            if budget is not None:
                budget.claim("tool")
            invoked = True
            handler_output = invoker(request.tool_name, arguments)
            output = str(handler_output)
            result = ToolExecutionResult(
                status=getattr(handler_output, "status", "success"),
                output=output,
                error_type=getattr(handler_output, "error_type", None),
                error_message=getattr(handler_output, "error_message", None),
                exit_code=getattr(handler_output, "exit_code", None),
                effect_state=getattr(handler_output, "effect_state", "unknown"),
                final_arguments=arguments,
                duration_ms=_elapsed_ms(started),
                metadata=dict(getattr(handler_output, "metadata", {}) or {}),
                pre_hook_trace=pre_traces,
            )
        except Exception as e:
            from runtime.execution.budget import ExecutionBudgetExceeded, ExecutionCancelled

            status = "cancelled" if isinstance(e, ExecutionCancelled) else (
                "timeout" if isinstance(e, ExecutionBudgetExceeded) else "error"
            )
            result = ToolExecutionResult(
                status=status,
                effect_state="unknown" if invoked else "not_started",
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
                    if outcome.updated_output is not None:
                        result.output = outcome.updated_output
                    if outcome.deny_reason:
                        result.status = "denied"
                        result.output = outcome.deny_reason
                        result.error_type = "ToolDenied"
                        result.error_message = outcome.deny_reason
                        result.sync_metadata()
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

        result.sync_metadata()

def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
