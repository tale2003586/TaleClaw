from __future__ import annotations

from dataclasses import dataclass

from modes.bot import BOT_PROFILE
from modes.coding import CODING_PROFILE
from .intent import IntentCandidate
from modes.base import ModeProfile


@dataclass(frozen=True)
class ExecutionPlan:
    intent: str
    execution: str
    profile: ModeProfile
    confidence: float = 1.0
    reason: str = ""
    switched: bool = False
    switch_message: str | None = None


class ExecutionPlanner:
    """Turn classified intent plus session state into an execution path."""

    def plan(self, candidate: IntentCandidate | None, session) -> ExecutionPlan:
        command = getattr(candidate, "command", None)
        if command == "coding":
            if not self.coding_allowed(session):
                session.set_mode("bot")
                return ExecutionPlan(
                    profile=BOT_PROFILE,
                    switched=True,
                    switch_message="当前账号没有 Coding 模式权限，已保持聊天模式。",
                    intent="mode_switch",
                    execution="direct_reply",
                    reason="Coding mode requires an admin role.",
                )
            session.set_mode("coding")
            return ExecutionPlan(
                profile=CODING_PROFILE,
                switched=True,
                switch_message="已进入编程模式。",
                intent="mode_switch",
                execution="direct_reply",
                reason="Explicit coding mode command.",
            )

        if command == "bot":
            session.set_mode("bot")
            return ExecutionPlan(
                profile=BOT_PROFILE,
                switched=True,
                switch_message="已回到聊天模式。",
                intent="mode_switch",
                execution="direct_reply",
                reason="Explicit bot mode command.",
            )

        if command == "hybrid":
            session.set_mode("hybrid")
            return ExecutionPlan(
                profile=BOT_PROFILE,
                switched=True,
                switch_message="已进入混合模式。",
                intent="mode_switch",
                execution="direct_reply",
                reason="Explicit hybrid mode command.",
            )

        if session.current_mode == "coding" and self.coding_allowed(session):
            return ExecutionPlan(
                profile=CODING_PROFILE,
                intent="coding",
                execution="task_session",
                reason="Session is pinned to coding mode.",
            )

        if session.current_mode == "coding" and not self.coding_allowed(session):
            session.set_mode("bot")
            return ExecutionPlan(
                profile=BOT_PROFILE,
                intent="chat",
                execution="pipeline_bot",
                confidence=0.55,
                reason="Coding mode was revoked because the user is not admin.",
            )

        if session.current_mode == "bot":
            return ExecutionPlan(
                profile=BOT_PROFILE,
                intent="chat",
                execution="pipeline_bot",
                reason="Session is pinned to bot mode.",
            )

        if candidate is not None and candidate.intent != "coding":
            return ExecutionPlan(
                profile=BOT_PROFILE,
                intent=candidate.intent,
                execution=candidate.execution,
                confidence=candidate.confidence,
                reason=candidate.reason,
            )

        if candidate is not None and not self.coding_allowed(session):
            return ExecutionPlan(
                profile=BOT_PROFILE,
                intent="chat",
                execution="pipeline_bot",
                confidence=0.55,
                reason="Coding candidate was downgraded because the user is not admin.",
            )

        return ExecutionPlan(
            profile=BOT_PROFILE,
            intent="chat",
            execution="pipeline_bot",
            reason="No specialized route matched.",
        )

    def coding_allowed(self, session) -> bool:
        metadata = getattr(session, "metadata", {}) or {}
        return metadata.get("user_role", "admin") == "admin"
