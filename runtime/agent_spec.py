from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentSpec:
    """Declarative identity and runtime choices for one agent execution."""

    name: str
    profile: Any
    model_purpose: str
    role: str = ""
    max_tokens: int | None = None
    max_reasoning_steps: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
