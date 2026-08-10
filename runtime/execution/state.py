"""Per-run control state; no task semantics or session transport belong here."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from runtime.execution.failure_reasons import StopDecision


@dataclass
class RunExecutionState:
    run_id: str = ""
    parent_run_id: str = ""
    input_text: str = ""
    thinking_enabled: bool = False
    model_profile: str = ""
    messages: list[Any] = field(default_factory=list)
    reasoning_step: int = 0
    tool_calls: int = 0
    active_tool_calls: set[str] = field(default_factory=set)
    web_search_limit: int = 0
    web_search_used: int = 0
    web_search_remaining: int = 0
    duplicate_fingerprints: dict[str, int] = field(default_factory=dict)
    recovery_attempts: int = 0
    task_state_version: int | None = None
    recovered_incidents: set[str] = field(default_factory=set)
    corrected_incidents: set[str] = field(default_factory=set)
    cancellation_requested: bool = False
    security_knowledge_used: bool = False
    finishing_reminder_sent: bool = False
    usage: dict[str, int] = field(default_factory=dict)
    stop_decision: StopDecision | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def stop_reason(self) -> str:
        return self.stop_decision.reason.value if self.stop_decision else ""

    @property
    def stop_message(self) -> str:
        return self.stop_decision.message if self.stop_decision else ""

    def reset(self, *, web_search_limit: int) -> None:
        self.reasoning_step = 0
        self.tool_calls = 0
        self.active_tool_calls.clear()
        self.web_search_limit = max(0, int(web_search_limit))
        self.web_search_used = 0
        self.web_search_remaining = self.web_search_limit
        self.duplicate_fingerprints.clear()
        self.recovery_attempts = 0
        self.task_state_version = None
        self.recovered_incidents.clear()
        self.corrected_incidents.clear()
        self.cancellation_requested = False
        self.security_knowledge_used = False
        self.finishing_reminder_sent = False
        self.usage.clear()
        self.stop_decision = None
        self.metadata.clear()


__all__ = ("RunExecutionState",)
