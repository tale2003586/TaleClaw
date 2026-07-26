from datetime import datetime, timedelta, timezone

from applications.minecraft.models import BotObservation, NearbyBlock, Position
from applications.minecraft.world_state import BeliefWorldState


def _observation(at, blocks=()):
    return BotObservation(
        observed_at=at.isoformat(),
        position=Position(x=0, y=64, z=0),
        nearby_blocks=tuple(blocks),
    )


def test_world_state_tracks_freshness_source_and_expiry():
    now = datetime.now(timezone.utc)
    block = NearbyBlock(
        block="oak_log",
        position=Position(x=2, y=64, z=0),
        distance=2,
    )
    world = BeliefWorldState(task_id="task").merge(
        _observation(now, (block,)),
        ttl_seconds=10,
    )
    resource = next(fact for key, fact in world.facts.items() if key.startswith("resource:"))
    assert resource.observed_at
    assert resource.expires_at
    assert resource.confidence == 1
    assert resource.source == "bridge"
    later = now + timedelta(seconds=20)
    world = world.merge(_observation(later), ttl_seconds=10)
    assert not any(key.startswith("resource:") for key in world.facts)


def test_failed_path_is_remembered_with_count():
    world = BeliefWorldState(task_id="task")
    destination = Position(x=10, y=64, z=5)
    world = world.record_failed_path(destination=destination, event_id="e1")
    world = world.record_failed_path(destination=destination, event_id="e2")
    fact = next(fact for key, fact in world.facts.items() if key.startswith("failed_path:"))
    assert fact.failure_count == 2
