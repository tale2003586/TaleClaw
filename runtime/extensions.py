"""Optional services composed by applications around the core Runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ContextContribution:
    """Application-owned, read-only material offered to the context assembler."""

    name: str
    content: str
    source: str = "application"


class ContextContributor(Protocol):
    def contribute(self, *, session: Any, profile: Any) -> list[ContextContribution]: ...


@dataclass(frozen=True)
class RuntimeExtensions:
    memory: Any = None
    retrieval: Any = None
    artifacts: Any = None
    trace: Any = None
    message_bus: Any = None
    context_contributors: tuple[ContextContributor, ...] = ()

    def enabled(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in ("memory", "retrieval", "artifacts", "trace", "message_bus")
            if getattr(self, name) is not None
        )


__all__ = (
    "ContextContribution",
    "ContextContributor",
    "RuntimeExtensions",
)
