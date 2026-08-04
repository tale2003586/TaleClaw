from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ToolRisk(_StringEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class ToolInjection(_StringEnum):
    ALWAYS = "always"
    PRELOADED = "preloaded"
    DEFERRED = "deferred"


class ToolStateEffect(_StringEnum):
    NONE = "none"
    EXTERNAL = "external"
    AGENT_STATE = "agent_state"
    RUNTIME_STATE = "runtime_state"


@dataclass
class ToolSpec:
    """The sole runtime authority for a tool's schema and behavior.

    Providers and plugins register this object directly. Visibility, policy,
    recovery and tracing all read the same instance; none maintains a parallel
    risk or side-effect table.
    """

    schema: dict[str, Any]
    handler: Callable[..., str]
    allowed_modes: frozenset[str] = field(
        default_factory=lambda: frozenset({"bot", "coding", "teammate"})
    )
    risk: ToolRisk = ToolRisk.NORMAL
    idempotent: bool = True
    side_effect: bool = False
    state_effect: ToolStateEffect = ToolStateEffect.NONE
    injection: ToolInjection = ToolInjection.PRELOADED
    source: str = "local"
    session_scoped: bool = False
    admin_only: bool = False
    requires_audit: bool = False
    policy_tag: str = ""
    runtime_parameters: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        self.allowed_modes = frozenset(self.allowed_modes)
        self.risk = ToolRisk(self.risk)
        self.state_effect = ToolStateEffect(self.state_effect)
        self.injection = ToolInjection(self.injection)
        self.runtime_parameters = frozenset(self.runtime_parameters)
        if self.side_effect or self.state_effect is not ToolStateEffect.NONE:
            self.requires_audit = True

    @property
    def name(self) -> str:
        return str(self.schema["function"]["name"])

    @property
    def description(self) -> str:
        return str(self.schema["function"].get("description") or "")

    def enabled_for(self, mode: str, session=None) -> bool:
        if mode not in self.allowed_modes:
            return False
        if self.admin_only and session is not None:
            metadata = getattr(session, "metadata", {}) or {}
            return metadata.get("user_role", "admin") == "admin"
        return True

    def governance_dict(self) -> dict[str, object]:
        return {
            "risk": self.risk.value,
            "idempotent": self.idempotent,
            "side_effect": self.side_effect,
            "state_effect": self.state_effect.value,
            "requires_audit": self.requires_audit,
            "allowed_modes": sorted(self.allowed_modes),
            "injection": self.injection.value,
            "policy_tag": self.policy_tag,
            "session_scoped": self.session_scoped,
            "admin_only": self.admin_only,
            "runtime_parameters": sorted(self.runtime_parameters),
        }
