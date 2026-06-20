from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SubagentResult:
    agent_type: str
    success: bool
    summary: str
    status: str = "completed"
    files_touched: list[str] = field(default_factory=list)
    tool_count: int = 0
    error: str | None = None
    truncated: bool = False
    stop_reason: str | None = None
    findings: list[dict[str, Any]] = field(default_factory=list)
    incomplete: bool = False
    failure_reason: str | None = None
    failure_message: str | None = None
    recoverable: bool = False
    retry_hint: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
