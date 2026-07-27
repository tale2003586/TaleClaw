from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ToolScope(_StringEnum):
    USER = "user"
    KERNEL = "kernel"


class ToolStateEffect(_StringEnum):
    NONE = "none"
    EXTERNAL = "external"
    AGENT_STATE = "agent_state"
    RUNTIME_STATE = "runtime_state"


class ToolRiskLevel(_StringEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MemoryEffect(_StringEnum):
    NONE = "none"
    READ = "read"
    WRITE = "write"
    PROMOTE = "promote"
    EVOLVE = "evolve"
    ARCHIVE = "archive"


class ContextEffect(_StringEnum):
    NONE = "none"
    INJECT = "inject"
    COMPRESS = "compress"
    EVICT = "evict"
    CHECKPOINT = "checkpoint"


@dataclass(frozen=True)
class ToolGovernanceMetadata:
    """Runtime-only tool classification; never exposed in model tool schemas."""

    tool_scope: ToolScope = ToolScope.USER
    state_effect: ToolStateEffect = ToolStateEffect.NONE
    risk_level: ToolRiskLevel = ToolRiskLevel.LOW
    requires_audit: bool = False
    allowed_modes: tuple[str, ...] = ()
    policy_tag: str = ""
    memory_effect: MemoryEffect = MemoryEffect.NONE
    context_effect: ContextEffect = ContextEffect.NONE

    def __post_init__(self) -> None:
        if self.tool_scope is ToolScope.KERNEL and not self.requires_audit:
            object.__setattr__(self, "requires_audit", True)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        for key, value in tuple(payload.items()):
            if isinstance(value, Enum):
                payload[key] = value.value
        payload["allowed_modes"] = list(self.allowed_modes)
        return payload


def governance_for_tool(name: str) -> ToolGovernanceMetadata:
    """Return explicit classifications for runtime-owned tools.

    Unknown tools retain inert user-tool defaults, so third-party registrations do
    not acquire new policy or visibility behavior.
    """
    if name == "memorize":
        return ToolGovernanceMetadata(
            tool_scope=ToolScope.KERNEL,
            state_effect=ToolStateEffect.AGENT_STATE,
            risk_level=ToolRiskLevel.MEDIUM,
            policy_tag="memory.write",
            memory_effect=MemoryEffect.WRITE,
        )
    if name == "recall_memory":
        return ToolGovernanceMetadata(
            tool_scope=ToolScope.KERNEL,
            risk_level=ToolRiskLevel.LOW,
            policy_tag="memory.read",
            memory_effect=MemoryEffect.READ,
            context_effect=ContextEffect.INJECT,
        )
    return ToolGovernanceMetadata()
