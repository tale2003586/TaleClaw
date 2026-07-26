from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tools.schema import function_tool
from tools.tool_registry import ToolRegistry


@dataclass
class RecordingTool:
    output: str = "tool-ok"
    error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.output


def registry_with_tool(
    name: str,
    handler,
    *,
    modes: set[str] | None = None,
    admin_only: bool = False,
    always_on: bool = True,
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        function_tool(
            name,
            f"Deterministic {name} test tool.",
            {"value": {"type": "integer"}},
            [],
        ),
        handler,
        allowed_agents=modes,
        admin_only=admin_only,
        always_on=always_on,
    )
    return registry
