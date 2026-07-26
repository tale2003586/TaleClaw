from __future__ import annotations

from dataclasses import dataclass

from agents.definitions import BOT_AGENT_SPEC
from agents.definitions import CODING_AGENT_SPEC
from runtime.agent_spec import AgentSpec
from .intent import IntentCandidate


@dataclass(frozen=True)
class ExecutionPlan:
    intent: str
    execution: str
    agent_spec: AgentSpec
    confidence: float = 1.0
    reason: str = ""
    switched: bool = False
    switch_message: str | None = None

    @property
    def profile(self) -> AgentSpec:
        return self.agent_spec


class ExecutionPlanner:
    """Turn classified intent plus session state into an execution path."""

    def plan(self, candidate: IntentCandidate | None, session) -> ExecutionPlan:
        selected_agent = (
            session.selected_agent()
            if hasattr(session, "selected_agent")
            else session.active_agent
        )
        command = getattr(candidate, "command", None)
        if command == "coding":
            if not self.coding_allowed(session):
                session.set_mode("bot")
                return ExecutionPlan(
                    agent_spec=BOT_AGENT_SPEC,
                    switched=True,
                    switch_message="当前账号没有 Coding 模式权限，已保持聊天模式。",
                    intent="mode_switch",
                    execution="direct_reply",
                    reason="Coding mode requires an admin role.",
                )
            session.set_mode("coding")
            return ExecutionPlan(
                agent_spec=CODING_AGENT_SPEC,
                switched=True,
                switch_message="已进入编程模式。",
                intent="mode_switch",
                execution="direct_reply",
                reason="Explicit coding mode command.",
            )

        if command == "bot":
            session.set_mode("bot")
            return ExecutionPlan(
                agent_spec=BOT_AGENT_SPEC,
                switched=True,
                switch_message="已回到聊天模式。",
                intent="mode_switch",
                execution="direct_reply",
                reason="Explicit bot mode command.",
            )

        if command == "hybrid":
            session.set_mode("hybrid")
            return ExecutionPlan(
                agent_spec=BOT_AGENT_SPEC,
                switched=True,
                switch_message="已进入混合模式。",
                intent="mode_switch",
                execution="direct_reply",
                reason="Explicit hybrid mode command.",
            )

        if selected_agent == "coding" and self.coding_allowed(session):
            return ExecutionPlan(
                agent_spec=CODING_AGENT_SPEC,
                intent="coding",
                execution="coding_application",
                reason="Session is pinned to coding mode.",
            )

        if selected_agent == "coding" and not self.coding_allowed(session):
            session.set_mode("bot")
            return ExecutionPlan(
                agent_spec=BOT_AGENT_SPEC,
                intent="chat",
                execution="runtime",
                confidence=0.55,
                reason="Coding mode was revoked because the user is not admin.",
            )

        if selected_agent == "bot":
            return ExecutionPlan(
                agent_spec=BOT_AGENT_SPEC,
                intent="chat",
                execution="runtime",
                reason="Session is pinned to bot mode.",
            )

        if candidate is not None and candidate.intent != "coding":
            return ExecutionPlan(
                agent_spec=BOT_AGENT_SPEC,
                intent=candidate.intent,
                execution=candidate.execution,
                confidence=candidate.confidence,
                reason=candidate.reason,
            )

        if candidate is not None and not self.coding_allowed(session):
            return ExecutionPlan(
                agent_spec=BOT_AGENT_SPEC,
                intent="chat",
                execution="runtime",
                confidence=0.55,
                reason="Coding candidate was downgraded because the user is not admin.",
            )

        return ExecutionPlan(
            agent_spec=BOT_AGENT_SPEC,
            intent="chat",
            execution="runtime",
            reason="No specialized route matched.",
        )

    def coding_allowed(self, session) -> bool:
        metadata = getattr(session, "metadata", {}) or {}
        return metadata.get("user_role", "admin") == "admin"
