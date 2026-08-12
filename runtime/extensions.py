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
    def contribute(self, *, session: Any, agent_spec: Any) -> list[ContextContribution]: ...


class RunObserver(Protocol):
    def state_version(self, session: Any) -> int | None: ...

    def after_run(self, *, session: Any, execution: Any) -> None: ...


@dataclass(frozen=True)
class RuntimeExtensions:
    memory: Any = None
    retrieval: Any = None
    artifacts: Any = None
    trace: Any = None
    message_bus: Any = None
    context_contributors: tuple[ContextContributor, ...] = ()
    run_observers: tuple[RunObserver, ...] = ()

    def enabled(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in ("memory", "retrieval", "artifacts", "trace", "message_bus")
            if getattr(self, name) is not None
        )


__all__ = (
    "ContextContribution",
    "ContextContributor",
    "RunObserver",
    "RuntimeExtensions",
)
