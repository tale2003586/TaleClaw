from __future__ import annotations

from dataclasses import dataclass

from .models import BotObservation


@dataclass(frozen=True)
class SafetyDecision:
    interrupt: bool
    reason_code: str = ""
    recovery_action: str = ""


class SafetyController:
    def __init__(
        self,
        *,
        minimum_health: float = 7,
        minimum_food: int = 5,
        minimum_oxygen: int = 50,
    ) -> None:
        self.minimum_health = minimum_health
        self.minimum_food = minimum_food
        self.minimum_oxygen = minimum_oxygen

    def evaluate(self, observation: BotObservation) -> SafetyDecision:
        if observation.hazards.lava:
            return SafetyDecision(True, "lava_hazard", "return_safe")
        if observation.hazards.fall:
            return SafetyDecision(True, "fall_hazard", "stop")
        if observation.oxygen <= self.minimum_oxygen:
            return SafetyDecision(True, "low_oxygen", "return_safe")
        if observation.health <= self.minimum_health:
            return SafetyDecision(True, "low_health", "return_safe")
        if observation.food <= self.minimum_food:
            return SafetyDecision(True, "low_food", "eat")
        return SafetyDecision(False)
