from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class ModelPolicy:
    """Declarative model route for one agent."""

    purpose: str = "chat"
    model: str | None = None


@dataclass(frozen=True)
class ToolSet:
    """The tool view requested by an agent; final authorization stays in ToolRegistry."""

    mode: str = "bot"
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextPolicy:
    """Named context construction policy without carrying mutable context state."""

    name: str = "default"
    include_memory: bool = True
    include_history: bool = True
    include_skills: bool = True


@dataclass(frozen=True)
class TerminationPolicy:
    """Declarative stopping behavior interpreted by the current runner."""

    name: str = "default"
    allow_empty_final: bool = False


@dataclass(frozen=True)
class RunLimits:
    max_tokens: int | None = None
    max_reasoning_steps: int | None = None
    max_tool_calls: int | None = None

    def __post_init__(self) -> None:
        for name in ("max_tokens", "max_reasoning_steps", "max_tool_calls"):
            value = getattr(self, name)
            if value is not None and int(value) < 1:
                raise ValueError(f"{name} must be positive when provided.")


@dataclass(frozen=True)
class SpawnPolicy:
    enabled: bool = False
    allowed_agent_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentSpec:
    """Immutable definition of an agent, separate from Session and per-run state."""

    name: str
    model_purpose: str = ""
    role: str = ""
    max_tokens: int | None = None
    max_reasoning_steps: int | None = None
    thinking_enabled: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    instructions: str = ""
    model_policy: ModelPolicy | None = None
    tool_set: ToolSet | None = None
    context_policy: ContextPolicy = field(default_factory=ContextPolicy)
    termination_policy: TerminationPolicy = field(default_factory=TerminationPolicy)
    limits: RunLimits | None = None
    output_schema: Any = None
    skills: tuple[str, ...] = ()
    hooks: tuple[Any, ...] = ()
    spawn_policy: SpawnPolicy = field(default_factory=SpawnPolicy)

    def __post_init__(self) -> None:
        if not str(self.name or "").strip():
            raise ValueError("AgentSpec.name is required.")
        instructions = self.instructions
        purpose = self.model_purpose or "chat"
        model_policy = self.model_policy or ModelPolicy(purpose=purpose)
        tool_set = self.tool_set or ToolSet()
        limits = self.limits or RunLimits(
            max_tokens=self.max_tokens,
            max_reasoning_steps=self.max_reasoning_steps,
        )
        object.__setattr__(self, "instructions", instructions)
        object.__setattr__(self, "model_purpose", model_policy.purpose)
        object.__setattr__(self, "model_policy", model_policy)
        object.__setattr__(self, "tool_set", tool_set)
        object.__setattr__(self, "limits", limits)
        object.__setattr__(self, "max_tokens", limits.max_tokens)
        object.__setattr__(
            self,
            "max_reasoning_steps",
            limits.max_reasoning_steps,
        )
        object.__setattr__(self, "metadata", dict(self.metadata or {}))
        object.__setattr__(self, "skills", tuple(self.skills or ()))
        object.__setattr__(self, "hooks", tuple(self.hooks or ()))

    def with_limits(
        self,
        *,
        max_tokens: int | None = None,
        max_reasoning_steps: int | None = None,
        max_tool_calls: int | None = None,
    ) -> "AgentSpec":
        current = self.limits or RunLimits()
        return replace(
            self,
            limits=RunLimits(
                max_tokens=max_tokens if max_tokens is not None else current.max_tokens,
                max_reasoning_steps=(
                    max_reasoning_steps
                    if max_reasoning_steps is not None
                    else current.max_reasoning_steps
                ),
                max_tool_calls=(
                    max_tool_calls
                    if max_tool_calls is not None
                    else current.max_tool_calls
                ),
            ),
        )

    @property
    def tool_mode(self) -> str:
        return str(getattr(self.tool_set, "mode", "bot") or "bot")
