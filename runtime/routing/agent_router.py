from dataclasses import dataclass

from agents.definitions import BOT_AGENT_SPEC
from agents.definitions import CODING_AGENT_SPEC
from runtime.agent_spec import AgentSpec
from .execution_plan import ExecutionPlan, ExecutionPlanner
from .intent import IntentClassifier


@dataclass
class RouteResult:
    agent_spec: AgentSpec
    switched: bool = False
    switch_message: str | None = None
    intent: str = "chat"
    execution: str = "runtime"
    confidence: float = 1.0
    reason: str = ""

    @property
    def profile(self) -> AgentSpec:
        return self.agent_spec


class AgentRouter:
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
            and plan.agent_spec is BOT_AGENT_SPEC
            and plan.reason == "No specialized route matched."
            and self._coding_allowed(session)
        ):
            if (
                self.hybrid_classifier is not None
                and self.hybrid_classifier.should_use_coding(user_text)
            ):
                plan = ExecutionPlan(
                    agent_spec=CODING_AGENT_SPEC,
                    intent="coding",
                    execution="coding_application",
                    confidence=0.86,
                    reason="Coding candidate accepted by the hybrid classifier.",
                )
            else:
                plan = ExecutionPlan(
                    agent_spec=BOT_AGENT_SPEC,
                    intent="chat",
                    execution="runtime",
                    confidence=0.58,
                    reason="Coding candidate was not accepted by the hybrid classifier.",
                )

        return self._record(session, self._route_result(plan))

    def _coding_allowed(self, session) -> bool:
        return self.execution_planner.coding_allowed(session)

    def _route_result(self, plan: ExecutionPlan) -> RouteResult:
        return RouteResult(
            agent_spec=plan.agent_spec,
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
                "agent": result.agent_spec.name if result.agent_spec else "",
                "confidence": result.confidence,
                "reason": result.reason,
                "switched": result.switched,
            }
        return result
