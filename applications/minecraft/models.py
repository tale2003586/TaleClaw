from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskStatus(StrEnum):
    PENDING = "pending"
    CONNECTING = "connecting"
    OBSERVING = "observing"
    PLANNING = "planning"
    EXECUTING = "executing"
    RECOVERING = "recovering"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.FAILED,
            self.BLOCKED,
            self.CANCELLED,
        }


class ActionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PROGRESS = "progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED}


class BridgeActionType(StrEnum):
    OBSERVE = "observe"
    FIND_BLOCKS = "find_blocks"
    COLLECT_BLOCKS = "collect_blocks"
    CRAFT = "craft"
    SMELT = "smelt"
    EQUIP = "equip"
    EAT = "eat"
    RETURN_SAFE = "return_safe"
    BRANCH_MINE = "branch_mine"
    CANCEL_ACTION = "cancel_action"


class PlanSource(StrEnum):
    MODEL = "model"
    REVISION = "revision"
    FALLBACK = "fallback"


class CognitiveDecisionType(StrEnum):
    CONTINUE_PLAN = "continue_plan"
    EXECUTE_NEXT_STEP = "execute_next_step"
    LOCAL_RECOVERY = "local_recovery"
    CALL_PLANNER = "call_planner"
    CALL_LLM_CRITIC = "call_llm_critic"
    SAFETY_INTERRUPT = "safety_interrupt"
    COMPLETE_TASK = "complete_task"
    BLOCK_TASK = "block_task"
    FAIL_TASK = "fail_task"


class LocalEvaluationType(StrEnum):
    CONTINUE = "continue"
    STEP_SUCCEEDED = "step_succeeded"
    LOCAL_RECOVERY = "local_recovery"
    ESCALATE = "escalate"
    TASK_COMPLETED = "task_completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class ResourceGoal(StrictModel):
    resource: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=4096)
    original_text: str = Field(default="", max_length=500)


class Position(StrictModel):
    x: float
    y: float
    z: float


class InventoryItem(StrictModel):
    item: str = Field(min_length=1, max_length=96)
    count: int = Field(ge=0, le=2304)
    slot: int | None = Field(default=None, ge=0, le=255)
    durability_remaining: int | None = Field(default=None, ge=0)


class NearbyBlock(StrictModel):
    block: str = Field(min_length=1, max_length=96)
    position: Position
    distance: float = Field(ge=0, le=256)


class HazardSummary(StrictModel):
    lava: bool = False
    fall: bool = False
    drowning: bool = False
    hostile_mob_count: int = Field(default=0, ge=0, le=128)


class BotObservation(StrictModel):
    observed_at: str = Field(default_factory=now_iso)
    connected: bool = True
    bot_id: str = Field(default="", max_length=96)
    server_id: str = Field(default="", max_length=256)
    world_id: str = Field(default="", max_length=256)
    version: str = Field(default="", max_length=32)
    position: Position
    dimension: str = Field(default="overworld", max_length=64)
    health: float = Field(default=20, ge=0, le=20)
    food: int = Field(default=20, ge=0, le=20)
    oxygen: int = Field(default=300, ge=0, le=300)
    inventory: tuple[InventoryItem, ...] = Field(default_factory=tuple, max_length=128)
    equipment: tuple[InventoryItem, ...] = Field(default_factory=tuple, max_length=16)
    nearby_blocks: tuple[NearbyBlock, ...] = Field(default_factory=tuple, max_length=128)
    nearby_drops: tuple[InventoryItem, ...] = Field(default_factory=tuple, max_length=64)
    hazards: HazardSummary = Field(default_factory=HazardSummary)
    current_action_id: str | None = Field(default=None, max_length=128)
    current_action_status: ActionStatus | None = None

    def item_count(self, item: str) -> int:
        return sum(entry.count for entry in self.inventory if entry.item == item)


class BridgeAction(StrictModel):
    type: BridgeActionType
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=160)
    timeout_seconds: float = Field(default=30, gt=0, le=300)

    @field_validator("arguments")
    @classmethod
    def bounded_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 16:
            raise ValueError("action arguments exceed the field limit")
        rendered = str(value)
        if len(rendered) > 4096:
            raise ValueError("action arguments are too large")
        forbidden = {"code", "javascript", "python", "packet", "raw_packet", "shell"}
        if forbidden.intersection(str(key).lower() for key in value):
            raise ValueError("raw code or protocol arguments are forbidden")
        return value


class PlanStep(StrictModel):
    step_id: str = Field(min_length=1, max_length=96)
    goal: str = Field(min_length=1, max_length=300)
    action: BridgeActionType
    arguments: dict[str, Any] = Field(default_factory=dict)
    preconditions: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    success_conditions: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    fallback: Literal["local_recovery", "reobserve_and_replan", "block"] = (
        "reobserve_and_replan"
    )

    @field_validator("arguments")
    @classmethod
    def safe_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        return BridgeAction(
            type=BridgeActionType.OBSERVE,
            arguments=value,
            idempotency_key="validate-arguments",
        ).arguments


class TaskPlan(StrictModel):
    plan_version: int = Field(gt=0)
    source: PlanSource
    situation: str = Field(default="", max_length=1000)
    strategy: str = Field(min_length=1, max_length=2000)
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=12)
    replan_when: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    triggering_event_id: str = Field(min_length=1, max_length=128)


class CognitiveDecision(StrictModel):
    decision: CognitiveDecisionType
    reason_code: str = Field(min_length=1, max_length=96)
    event_id: str = Field(min_length=1, max_length=128)
    plan_version: int = Field(ge=0)
    consumes_model_budget: bool = False
    cancel_current_action: bool = False
    suggested_next_step_type: str = Field(default="", max_length=96)


class LocalEvaluation(StrictModel):
    result: LocalEvaluationType
    reason_code: str = Field(min_length=1, max_length=96)
    progress_delta: int = 0
    retryable: bool = False


class ActionHandle(StrictModel):
    action_id: str = Field(min_length=1, max_length=128)
    status: ActionStatus


class ActionEvent(StrictModel):
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    action_id: str = Field(min_length=1, max_length=128)
    status: ActionStatus
    progress: float | None = Field(default=None, ge=0, le=1)
    error_code: str | None = Field(default=None, max_length=96)
    message: str = Field(default="", max_length=500)
    observation: BotObservation | None = None


class TaskBudget(StrictModel):
    total_seconds: int = Field(default=900, gt=0, le=86400)
    action_count: int = Field(default=100, gt=0, le=10000)
    model_calls: int = Field(default=20, ge=0, le=1000)
    retries_per_error: int = Field(default=2, ge=0, le=20)
    search_distance: int = Field(default=64, gt=0, le=2048)
    mined_blocks: int = Field(default=256, gt=0, le=100000)


class MinecraftTask(StrictModel):
    task_id: str = Field(default_factory=lambda: f"mc_{uuid4().hex[:16]}")
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=256)
    bot_id: str = Field(min_length=1, max_length=96)
    goal: ResourceGoal
    status: TaskStatus = TaskStatus.PENDING
    baseline_count: int = Field(default=0, ge=0)
    current_count: int = Field(default=0, ge=0)
    plan_version: int = Field(default=0, ge=0)
    version: int = Field(default=0, ge=0)
    active_action_id: str | None = Field(default=None, max_length=128)
    cancel_requested: bool = False
    cancellation_scope: str = Field(default="", max_length=256)
    trace_id: str = Field(default="", max_length=128)
    budget: TaskBudget = Field(default_factory=TaskBudget)
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    error_code: str | None = Field(default=None, max_length=96)
    error_message: str = Field(default="", max_length=500)

    @property
    def net_acquired(self) -> int:
        return max(0, self.current_count - self.baseline_count)


class MinecraftTaskEvent(StrictModel):
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    task_id: str
    event_type: str = Field(min_length=1, max_length=96)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_iso)


class MinecraftCheckpoint(StrictModel):
    task_id: str
    version: int = Field(ge=0)
    plan_version: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_iso)


class WorkerLease(StrictModel):
    task_id: str
    owner_id: str = Field(min_length=1, max_length=128)
    expires_at: str


class TerminalReport(StrictModel):
    task_id: str
    status: Literal["succeeded", "failed", "blocked", "cancelled"]
    resource: str
    requested_quantity: int
    net_acquired: int
    elapsed_seconds: float = Field(ge=0)
    reason_code: str
    stage_summary: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    inventory_summary: dict[str, int] = Field(default_factory=dict)
    next_step: str = Field(default="", max_length=500)
