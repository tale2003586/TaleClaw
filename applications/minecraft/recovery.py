from __future__ import annotations

from dataclasses import dataclass

from .models import BridgeActionType


@dataclass(frozen=True)
class RecoveryDecision:
    retryable: bool
    action: BridgeActionType | None
    reason_code: str


class RecoveryController:
    """Deterministic, bounded recovery taxonomy used before any LLM escalation."""

    _LOCAL = {
        "path_failed": BridgeActionType.RETURN_SAFE,
        "tool_broken": BridgeActionType.EQUIP,
        "inventory_full": BridgeActionType.RETURN_SAFE,
        "low_food": BridgeActionType.EAT,
        "disconnected": BridgeActionType.OBSERVE,
        "bot_died": BridgeActionType.OBSERVE,
        "resource_not_found": BridgeActionType.FIND_BLOCKS,
    }

    def decide(self, error_code: str, *, attempts: int, limit: int) -> RecoveryDecision:
        code = str(error_code or "unknown_action_failure")
        action = self._LOCAL.get(code)
        if action is None:
            return RecoveryDecision(False, None, code)
        if attempts >= max(0, int(limit)):
            return RecoveryDecision(False, None, "recovery_exhausted")
        return RecoveryDecision(True, action, code)
