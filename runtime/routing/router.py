from dataclasses import dataclass

from modes.base import ModeProfile
from modes.bot import BOT_PROFILE
from modes.coding import CODING_PROFILE
from .execution_plan import ExecutionPlan, ExecutionPlanner
from .intent import IntentClassifier


@dataclass
class RouteResult:
    profile: ModeProfile
    switched: bool = False
    switch_message: str | None = None
    intent: str = "chat"
    execution: str = "pipeline_bot"
    confidence: float = 1.0
    reason: str = ""


class ModeRouter:
    def __init__(
        self,
        *,
        hybrid_classifier=None,
        intent_classifier: IntentClassifier | None = None,
        execution_planner: ExecutionPlanner | None = None,
    ) -> None:
        self.hybrid_classifier = hybrid_classifier
        self.intent_classifier = intent_classifier or IntentClassifier()
        self.execution_planner = execution_planner or ExecutionPlanner()

    def route(self, session, user_text: str) -> RouteResult:
        candidate = self.intent_classifier.classify(user_text, session)
        plan = self.execution_planner.plan(candidate, session)

        if (
            candidate is not None
            and candidate.intent == "coding"
            and plan.profile is BOT_PROFILE
            and plan.reason == "No specialized route matched."
            and self._coding_allowed(session)
        ):
            if (
                self.hybrid_classifier is not None
                and self.hybrid_classifier.should_use_coding(user_text)
            ):
                plan = ExecutionPlan(
                    profile=CODING_PROFILE,
                    intent="coding",
                    execution="task_session",
                    confidence=0.86,
                    reason="Coding candidate accepted by the hybrid classifier.",
                )
            else:
                plan = ExecutionPlan(
                    profile=BOT_PROFILE,
                    intent="chat",
                    execution="pipeline_bot",
                    confidence=0.58,
                    reason="Coding candidate was not accepted by the hybrid classifier.",
                )

        return self._record(session, self._route_result(plan))

    def _coding_allowed(self, session) -> bool:
        return self.execution_planner.coding_allowed(session)

    def _route_result(self, plan: ExecutionPlan) -> RouteResult:
        return RouteResult(
            profile=plan.profile,
            switched=plan.switched,
            switch_message=plan.switch_message,
            intent=plan.intent,
            execution=plan.execution,
            confidence=plan.confidence,
            reason=plan.reason,
        )

    def _record(self, session, result: RouteResult) -> RouteResult:
        metadata = getattr(session, "metadata", None)
        if isinstance(metadata, dict):
            metadata["last_route"] = {
                "intent": result.intent,
                "execution": result.execution,
                "profile": result.profile.name,
                "tool_mode": result.profile.tool_mode,
                "confidence": result.confidence,
                "reason": result.reason,
                "switched": result.switched,
            }
        return result
