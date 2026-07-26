from __future__ import annotations

from pydantic import Field

from .catalog import DomainCatalog
from .context import MinecraftCriticContextBuilder
from .model_gateway import MinecraftModelGateway
from .models import (
    CognitiveDecision,
    CognitiveDecisionType,
    MinecraftTask,
    StrictModel,
    TaskPlan,
)
from .world_state import BeliefWorldState


class CriticResult(StrictModel):
    diagnosis: str = Field(min_length=1, max_length=1500)
    excluded_strategy: str = Field(default="", max_length=300)
    recommendation: str = Field(min_length=1, max_length=1500)


class LLMCritic:
    def __init__(
        self,
        *,
        gateway: MinecraftModelGateway,
        context_builder: MinecraftCriticContextBuilder,
        catalog: DomainCatalog,
    ) -> None:
        self.gateway = gateway
        self.context_builder = context_builder
        self.catalog = catalog

    def critique(
        self,
        *,
        task: MinecraftTask,
        world: BeliefWorldState,
        plan: TaskPlan,
        failures: tuple[dict, ...],
        approval: CognitiveDecision,
    ) -> CriticResult:
        if approval.decision is not CognitiveDecisionType.CALL_LLM_CRITIC:
            raise PermissionError("LLMCritic requires CALL_LLM_CRITIC approval")
        messages = self.context_builder.build_critic(
            task=task,
            world=world,
            catalog=self.catalog,
            plan=plan,
            failures=failures,
        )
        return self.gateway.run_structured(
            purpose="critic",
            messages=messages,
            schema=CriticResult,
            approval=approval,
            max_tokens=600,
        ).value
