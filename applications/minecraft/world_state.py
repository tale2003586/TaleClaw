from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import Field

from .models import BotObservation, Position, StrictModel, now_iso


class BeliefFact(StrictModel):
    key: str
    value: Any
    observed_at: str
    expires_at: str
    confidence: float = Field(ge=0, le=1)
    source: str = Field(min_length=1, max_length=96)
    failure_count: int = Field(default=0, ge=0, le=1000)

    def expired(self, at: datetime) -> bool:
        return datetime.fromisoformat(self.expires_at) <= at


class BeliefWorldState(StrictModel):
    task_id: str
    updated_at: str = Field(default_factory=now_iso)
    facts: dict[str, BeliefFact] = Field(default_factory=dict)
    observation_count: int = Field(default=0, ge=0)

    def merge(
        self,
        observation: BotObservation,
        *,
        ttl_seconds: int = 120,
        source: str = "bridge",
    ) -> "BeliefWorldState":
        observed = datetime.fromisoformat(observation.observed_at)
        expires = (observed + timedelta(seconds=max(1, ttl_seconds))).isoformat()
        facts = {
            key: fact
            for key, fact in self.facts.items()
            if not fact.expired(observed) or key.startswith("failed_path:")
        }
        facts["position"] = _fact(
            "position",
            observation.position.model_dump(),
            observation.observed_at,
            expires,
            source,
        )
        facts["survival"] = _fact(
            "survival",
            {
                "health": observation.health,
                "food": observation.food,
                "oxygen": observation.oxygen,
                "hazards": observation.hazards.model_dump(),
            },
            observation.observed_at,
            expires,
            source,
        )
        facts["inventory"] = _fact(
            "inventory",
            {item.item: item.count for item in observation.inventory},
            observation.observed_at,
            expires,
            source,
        )
        for block in observation.nearby_blocks:
            position_key = _position_key(block.position)
            key = f"resource:{block.block}:{position_key}"
            facts[key] = _fact(
                key,
                {
                    "block": block.block,
                    "position": block.position.model_dump(),
                    "distance": block.distance,
                },
                observation.observed_at,
                expires,
                source,
            )
        return self.model_copy(
            update={
                "facts": facts,
                "updated_at": observation.observed_at,
                "observation_count": self.observation_count + 1,
            }
        )

    def record_failed_path(
        self,
        *,
        destination: Position,
        event_id: str,
        ttl_seconds: int = 600,
    ) -> "BeliefWorldState":
        at = datetime.now(timezone.utc)
        key = f"failed_path:{_position_key(destination)}"
        existing = self.facts.get(key)
        facts = dict(self.facts)
        facts[key] = BeliefFact(
            key=key,
            value={"destination": destination.model_dump(), "event_id": event_id},
            observed_at=at.isoformat(),
            expires_at=(at + timedelta(seconds=ttl_seconds)).isoformat(),
            confidence=1,
            source="action_result",
            failure_count=(existing.failure_count if existing else 0) + 1,
        )
        return self.model_copy(update={"facts": facts, "updated_at": at.isoformat()})

    def context_summary(self, *, max_chars: int = 6000) -> str:
        lines = [
            f"task_id={self.task_id}",
            f"observations={self.observation_count}",
        ]
        for key in sorted(self.facts):
            fact = self.facts[key]
            lines.append(
                f"{key}: {fact.value} "
                f"(confidence={fact.confidence}, expires_at={fact.expires_at}, "
                f"source={fact.source})"
            )
            if sum(len(line) + 1 for line in lines) >= max_chars:
                lines.append("...[belief state truncated]")
                break
        return "\n".join(lines)[:max_chars]


def _fact(key, value, observed_at, expires_at, source) -> BeliefFact:
    return BeliefFact(
        key=key,
        value=value,
        observed_at=observed_at,
        expires_at=expires_at,
        confidence=1,
        source=source,
    )


def _position_key(position: Position) -> str:
    return f"{round(position.x, 1)}:{round(position.y, 1)}:{round(position.z, 1)}"
