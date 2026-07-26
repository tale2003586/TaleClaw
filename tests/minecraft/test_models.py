import pytest
from pydantic import ValidationError

from applications.minecraft.models import (
    BotObservation,
    BridgeAction,
    BridgeActionType,
    Position,
    ResourceGoal,
)


def test_resource_goal_and_observation_are_strict_and_serializable():
    goal = ResourceGoal(resource="oak_log", quantity=4)
    assert ResourceGoal.model_validate_json(goal.model_dump_json()) == goal
    observation = BotObservation(position=Position(x=0, y=64, z=0))
    assert BotObservation.model_validate(observation.model_dump()) == observation


@pytest.mark.parametrize("quantity", [0, -1, 4097])
def test_resource_goal_rejects_invalid_quantity(quantity):
    with pytest.raises(ValidationError):
        ResourceGoal(resource="oak_log", quantity=quantity)


def test_action_rejects_code_or_protocol_arguments():
    with pytest.raises(ValidationError):
        BridgeAction(
            type=BridgeActionType.FIND_BLOCKS,
            arguments={"raw_packet": "bad"},
            idempotency_key="12345678",
        )
