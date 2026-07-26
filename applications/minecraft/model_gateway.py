from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from models.model_task_runner import ModelTaskResult, ModelTaskRunner
from runtime.agent_spec import AgentSpec, ModelPolicy, RunLimits, ToolSet

from .models import CognitiveDecision, CognitiveDecisionType


T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class StructuredModelResult:
    value: BaseModel
    usage: Any
    elapsed_ms: float
    raw_chars: int


class MinecraftModelError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class MinecraftModelGateway:
    def __init__(
        self,
        *,
        runner: ModelTaskRunner,
        timeout_seconds: float = 30,
        trace: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.runner = runner
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.trace = trace or (lambda _event, _payload: None)

    def run_structured(
        self,
        *,
        purpose: str,
        messages: list[dict],
        schema: type[T],
        approval: CognitiveDecision,
        max_tokens: int = 1000,
    ) -> StructuredModelResult:
        if approval.decision not in {
            CognitiveDecisionType.CALL_PLANNER,
            CognitiveDecisionType.CALL_LLM_CRITIC,
        } or not approval.consumes_model_budget:
            raise MinecraftModelError("reasoning_gate_approval_required")
        spec = AgentSpec(
            name=f"minecraft_{purpose}",
            instructions="Return strict JSON only.",
            model_policy=ModelPolicy(purpose=f"minecraft_{purpose}"),
            tool_set=ToolSet(mode="minecraft", allow=()),
            limits=RunLimits(max_tokens=max_tokens, max_reasoning_steps=1, max_tool_calls=1),
        )
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"minecraft-{purpose}")
        future = executor.submit(
            self._run,
            spec=spec,
            messages=messages,
            max_tokens=max_tokens,
        )
        try:
            result = future.result(timeout=self.timeout_seconds)
        except FutureTimeout as exc:
            future.cancel()
            raise MinecraftModelError("model_timeout") from exc
        except Exception as exc:
            raise MinecraftModelError("model_failure", str(exc)) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        raw = result.content.strip()
        try:
            parsed = schema.model_validate(json.loads(_strip_fence(raw)))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise MinecraftModelError("invalid_model_output", str(exc)) from exc
        trace_payload = {
            "purpose": purpose,
            "reason_code": approval.reason_code,
            "event_id": approval.event_id,
            "plan_version": approval.plan_version,
            "consumed_model_budget": approval.consumes_model_budget,
            "input_chars": sum(len(str(item.get("content") or "")) for item in messages),
            "output_chars": len(raw),
            "usage": _usage_dict(result.usage),
        }
        self.trace("MINECRAFT_MODEL_COMPLETED", trace_payload)
        return StructuredModelResult(
            value=parsed,
            usage=result.usage,
            elapsed_ms=result.elapsed_ms,
            raw_chars=len(raw),
        )

    def _run(self, *, spec, messages, max_tokens) -> ModelTaskResult:
        if hasattr(self.runner, "run_result"):
            return self.runner.run_result(
                spec=spec,
                messages=messages,
                max_tokens=max_tokens,
            )
        return ModelTaskResult(
            content=self.runner.run(
                spec=spec,
                messages=messages,
                max_tokens=max_tokens,
            )
        )


def _strip_fence(value: str) -> str:
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)
    return value


def _usage_dict(usage) -> dict:
    if usage is None:
        return {}
    if hasattr(usage, "__dict__"):
        return dict(usage.__dict__)
    if isinstance(usage, dict):
        return dict(usage)
    return {"value": str(usage)}
