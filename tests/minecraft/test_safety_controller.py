from applications.minecraft.models import (
    BotObservation,
    HazardSummary,
    Position,
)
from applications.minecraft.safety_controller import SafetyController


def observation(**updates):
    base = BotObservation(position=Position(x=0, y=64, z=0))
    return base.model_copy(update=updates)


def test_safety_interrupts_without_model():
    controller = SafetyController()
    assert not controller.evaluate(observation()).interrupt
    assert controller.evaluate(observation(health=5)).reason_code == "low_health"
    assert controller.evaluate(
        observation(hazards=HazardSummary(lava=True))
    ).reason_code == "lava_hazard"
