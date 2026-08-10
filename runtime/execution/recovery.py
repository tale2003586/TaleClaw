"""Bounded anomaly recovery for the reasoning loop."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from runtime.execution.failure_reasons import StopReason
from runtime.units.json_repair import repair_json_object


class RecoveryAction(str, Enum):
    CORRECT_ONCE = "correct_once"
    STOP = "stop"


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    reason: StopReason
    instruction: str = ""
    incident_id: str = ""


class RecoveryJudge:
    """A no-tool judge used only for retry-safe anomalous calls."""

    def decide(self, *, provider, model: str, incident: dict[str, Any]) -> RecoveryDecision:
        response = provider.chat(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a runtime recovery judge. Do not solve the task and do not "
                        "request tools. Decide whether one corrected retry can make progress. "
                        "Return JSON only: {action: correct_once|stop, instruction: string}."
                    ),
                },
                {"role": "user", "content": json.dumps(incident, ensure_ascii=False, default=str)},
            ],
            tools=[],
            tool_choice="none",
            max_tokens=240,
        )
        repaired = repair_json_object(str(response.content or ""))
        data = repaired.payload if repaired.ok and isinstance(repaired.payload, dict) else {}
        if str(data.get("action") or "stop").strip().lower() != RecoveryAction.CORRECT_ONCE.value:
            return RecoveryDecision(RecoveryAction.STOP, StopReason.RECOVERY_REJECTED)
        instruction = str(data.get("instruction") or "").strip()
        if not instruction:
            return RecoveryDecision(RecoveryAction.STOP, StopReason.RECOVERY_REJECTED)
        return RecoveryDecision(
            RecoveryAction.CORRECT_ONCE,
            StopReason.NO_PROGRESS,
            instruction=instruction,
        )


class RecoveryController:
    """Enforces one judge and one correction per anomaly incident."""

    def __init__(
        self,
        judge: RecoveryJudge | None = None,
        *,
        max_recoveries_per_run: int = 2,
    ) -> None:
        self.judge = judge or RecoveryJudge()
        self.max_recoveries_per_run = max(0, int(max_recoveries_per_run))

    def duplicate_tool_call(
        self,
        *,
        calls: Iterable[Any],
        specs: Iterable[Any],
        state,
        provider,
        model: str,
        error_type: str = "",
        result_hash: str = "",
        task_state_version: int | None = None,
    ) -> RecoveryDecision:
        payload = [
            {
                "name": str(getattr(call, "name", "")),
                "arguments": getattr(call, "arguments", {}) or {},
            }
            for call in calls
        ]
        version = (
            task_state_version
            if task_state_version is not None
            else getattr(state, "task_state_version", None)
        )
        fingerprint = {
            "tool_calls": payload,
            "error_type": str(error_type or ""),
            "result_hash": str(result_hash or ""),
            "task_state_version": version,
        }
        encoded = json.dumps(fingerprint, sort_keys=True, ensure_ascii=False, default=str)
        incident_id = "duplicate:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]
        if incident_id in state.corrected_incidents:
            return RecoveryDecision(
                RecoveryAction.STOP, StopReason.RECOVERY_EXHAUSTED, incident_id=incident_id
            )
        known_specs = [spec for spec in specs if spec is not None]
        if not known_specs or any(
            bool(getattr(spec, "side_effect", True))
            or not bool(getattr(spec, "idempotent", False))
            for spec in known_specs
        ):
            return RecoveryDecision(
                RecoveryAction.STOP,
                StopReason.REPEATED_SIDE_EFFECT_RISK,
                incident_id=incident_id,
            )
        if state.recovery_attempts >= self.max_recoveries_per_run:
            return RecoveryDecision(
                RecoveryAction.STOP,
                StopReason.RECOVERY_EXHAUSTED,
                incident_id=incident_id,
            )
        if incident_id in state.recovered_incidents:
            return RecoveryDecision(
                RecoveryAction.STOP, StopReason.RECOVERY_EXHAUSTED, incident_id=incident_id
            )
        state.recovered_incidents.add(incident_id)
        state.recovery_attempts += 1
        try:
            decision = self.judge.decide(
                provider=provider,
                model=model,
                incident={
                    "kind": "duplicate_tool_call",
                    "calls": payload,
                    "error_type": str(error_type or ""),
                    "result_hash": str(result_hash or ""),
                    "task_state_version": version,
                },
            )
        except Exception:
            decision = RecoveryDecision(RecoveryAction.STOP, StopReason.RECOVERY_REJECTED)
        if decision.action is RecoveryAction.CORRECT_ONCE:
            state.corrected_incidents.add(incident_id)
        return RecoveryDecision(
            decision.action,
            decision.reason,
            decision.instruction,
            incident_id,
        )


__all__ = (
    "RecoveryAction",
    "RecoveryController",
    "RecoveryDecision",
    "RecoveryJudge",
)
