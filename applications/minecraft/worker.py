from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from runtime.cancellation import CancellationRegistry

from .fixed_planner import wood_collection_actions
from .models import (
    ActionStatus,
    MinecraftTask,
    MinecraftTaskEvent,
    MinecraftCheckpoint,
    TaskStatus,
)
from .ports import (
    BridgeClient,
    MinecraftProgressPublisher,
    MinecraftTaskStore,
    MinecraftTraceSink,
    NullProgressPublisher,
    NullTraceSink,
)
from .state_machine import goal_completed, transition, with_observation_count
from .models import BridgeAction


@dataclass(frozen=True)
class WorkerResult:
    task: MinecraftTask
    stages: tuple[str, ...]


class MinecraftWorker:
    """Minecraft domain loop.

    This loop intentionally owns game task state and never invokes AgentLoop.
    """

    def __init__(
        self,
        *,
        store: MinecraftTaskStore,
        bridge: BridgeClient,
        cancellations: CancellationRegistry,
        trace: MinecraftTraceSink | None = None,
        progress: MinecraftProgressPublisher | None = None,
        planner=None,
        evaluator=None,
        reasoning_gate=None,
        critic=None,
        catalog=None,
        safety_controller=None,
        owner_id: str | None = None,
        lease_ttl_seconds: float = 30,
    ) -> None:
        self.store = store
        self.bridge = bridge
        self.cancellations = cancellations
        self.trace = trace or NullTraceSink()
        self.progress = progress or NullProgressPublisher()
        self.planner = planner
        self.evaluator = evaluator
        self.reasoning_gate = reasoning_gate
        self.critic = critic
        self.catalog = catalog
        self.safety_controller = safety_controller
        self.owner_id = owner_id or f"worker-{uuid4().hex[:16]}"
        self.lease_ttl_seconds = max(1, float(lease_ttl_seconds))
        self._task_reasoning_gates: dict[str, object] = {}

    async def run(self, task_id: str) -> WorkerResult:
        if not self.store.acquire_lease(
            task_id,
            owner_id=self.owner_id,
            ttl_seconds=self.lease_ttl_seconds,
        ):
            return WorkerResult(task=self._require(task_id), stages=("lease_denied",))
        try:
            if self.planner is not None:
                return await self.run_cognitive(task_id, _lease_acquired=True)
            return await self._run_basic(task_id)
        finally:
            self.store.release_lease(task_id, owner_id=self.owner_id)

    async def _run_basic(self, task_id: str) -> WorkerResult:
        task = self._require(task_id)
        token = self.cancellations.register(task.cancellation_scope)
        stages: list[str] = ["initial_observation"]
        cycle = 0
        actions_used = 0
        try:
            while not task.status.terminal:
                fresh = self._require(task_id)
                if fresh.cancel_requested or token.requested():
                    task = fresh
                    task = self._set_status(task, TaskStatus.CANCELLED, "user_cancelled")
                    break
                task = fresh
                if not self.store.renew_lease(
                    task_id,
                    owner_id=self.owner_id,
                    ttl_seconds=self.lease_ttl_seconds,
                ):
                    return WorkerResult(task=task, stages=tuple(stages + ["lease_lost"]))

                observation = await self.bridge.observe()
                self._checkpoint(task, observation)
                task = self._record_count(task, observation.item_count(task.goal.resource))
                if goal_completed(task):
                    if task.status is not TaskStatus.SUCCEEDED:
                        task = self._set_status(task, TaskStatus.SUCCEEDED, "goal_reached")
                    break

                if actions_used >= task.budget.action_count:
                    task = self._set_status(task, TaskStatus.BLOCKED, "action_budget_exhausted")
                    break

                if task.status in {
                    TaskStatus.PENDING,
                    TaskStatus.CONNECTING,
                    TaskStatus.OBSERVING,
                }:
                    task = self._set_status(task, TaskStatus.EXECUTING, "plan_started")

                actions = wood_collection_actions(task, cycle=cycle)
                if not actions:
                    task = self._set_status(task, TaskStatus.BLOCKED, "no_action_available")
                    break

                for action in actions:
                    if token.requested():
                        if task.active_action_id:
                            await self.bridge.cancel_action(task.active_action_id)
                        task = self._set_status(task, TaskStatus.CANCELLED, "user_cancelled")
                        break
                    actions_used += 1
                    handle = await self.bridge.submit_action(action)
                    task = self._set_active_action(task, handle.action_id)
                    self._emit(task, "ACTION_STARTED", {"action": action.type.value})
                    terminal = None
                    async for event in self.bridge.watch_action(handle.action_id, token):
                        if not self.store.renew_lease(
                            task_id,
                            owner_id=self.owner_id,
                            ttl_seconds=self.lease_ttl_seconds,
                        ):
                            await self.bridge.cancel_action(handle.action_id)
                            return WorkerResult(
                                task=self._require(task_id),
                                stages=tuple(stages + ["lease_lost"]),
                            )
                        terminal = event
                        self._emit(
                            task,
                            "ACTION_PROGRESS"
                            if not event.status.terminal
                            else "ACTION_COMPLETED",
                            {
                                "action_id": event.action_id,
                                "status": event.status.value,
                                "error_code": event.error_code,
                            },
                        )
                    fresh = self._require(task_id)
                    if fresh.cancel_requested or token.requested():
                        task = fresh
                        task = self._set_status(task, TaskStatus.CANCELLED, "user_cancelled")
                        break
                    task = self._set_active_action(task, None)
                    if terminal is None or terminal.status is not ActionStatus.SUCCEEDED:
                        task = self._set_status(
                            task,
                            TaskStatus.BLOCKED,
                            terminal.error_code if terminal else "action_no_result",
                        )
                        break

                cycle += 1
                stages.append("collect")

            return WorkerResult(task=task, stages=tuple(stages))
        finally:
            if task.status.terminal:
                self.cancellations.release(task.cancellation_scope)

    async def run_cognitive(
        self,
        task_id: str,
        *,
        _lease_acquired: bool = False,
        _rolling_cycle: int = 0,
        _actions_used: int = 0,
    ) -> WorkerResult:
        from .models import LocalEvaluationType
        from .world_state import BeliefWorldState

        if self.evaluator is None or self.reasoning_gate is None:
            raise RuntimeError("cognitive worker requires evaluator and reasoning_gate")
        if not _lease_acquired:
            if not self.store.acquire_lease(
                task_id,
                owner_id=self.owner_id,
                ttl_seconds=self.lease_ttl_seconds,
            ):
                return WorkerResult(task=self._require(task_id), stages=("lease_denied",))
        task = self._require(task_id)
        gate = self._task_reasoning_gates.get(task_id)
        if gate is None:
            gate = (
                self.reasoning_gate.fork(
                    model_call_budget=min(
                        task.budget.model_calls,
                        self.reasoning_gate.state.remaining_model_calls,
                    )
                )
                if hasattr(self.reasoning_gate, "fork")
                else self.reasoning_gate
            )
            self._task_reasoning_gates[task_id] = gate
        token = self.cancellations.register(task.cancellation_scope)
        stages: list[str] = ["initial_observation"]
        observation = await self.bridge.observe()
        self._checkpoint(task, observation)
        if self.safety_controller is not None:
            safety = self.safety_controller.evaluate(observation)
            if safety.interrupt:
                task = self._set_status(task, TaskStatus.BLOCKED, safety.reason_code)
                self._emit(
                    task,
                    "SAFETY_INTERRUPTED",
                    {
                        "reason_code": safety.reason_code,
                        "recovery_action": safety.recovery_action,
                    },
                )
                return WorkerResult(task=task, stages=tuple(stages))
        world = BeliefWorldState(task_id=task.task_id).merge(observation)
        decision = gate.decide(
            event_id=f"{task.task_id}:plan:{task.plan_version + 1}",
            event_type="new_task" if task.plan_version == 0 else "plan_exhausted",
            task=task,
            plan=None,
        )
        task = self._set_status(task, TaskStatus.PLANNING, "initial_planning")
        plan = self.planner.create_plan(
            task=task,
            world=world,
            approval=decision,
        )
        task = self._set_plan_version(task, plan.plan_version)
        task = self._set_status(task, TaskStatus.EXECUTING, "plan_validated")
        stages.append("planning")
        try:
            for index, step in enumerate(plan.steps):
                fresh = self._require(task_id)
                if fresh.cancel_requested or token.requested():
                    task = fresh
                    task = self._set_status(task, TaskStatus.CANCELLED, "user_cancelled")
                    break
                task = fresh
                if _actions_used >= task.budget.action_count:
                    task = self._set_status(
                        task, TaskStatus.BLOCKED, "action_budget_exhausted"
                    )
                    break
                if not self.store.renew_lease(
                    task_id,
                    owner_id=self.owner_id,
                    ttl_seconds=self.lease_ttl_seconds,
                ):
                    return WorkerResult(task=task, stages=tuple(stages + ["lease_lost"]))
                before = await self.bridge.observe()
                if self.safety_controller is not None:
                    safety = self.safety_controller.evaluate(before)
                    if safety.interrupt:
                        task = self._set_status(task, TaskStatus.BLOCKED, safety.reason_code)
                        self._emit(
                            task,
                            "SAFETY_INTERRUPTED",
                            {
                                "reason_code": safety.reason_code,
                                "recovery_action": safety.recovery_action,
                            },
                        )
                        break
                action = BridgeAction(
                    type=step.action,
                    arguments=step.arguments,
                    idempotency_key=f"{task.task_id}:{plan.plan_version}:{step.step_id}",
                )
                handle = await self.bridge.submit_action(action)
                _actions_used += 1
                task = self._set_active_action(task, handle.action_id)
                terminal = None
                async for event in self.bridge.watch_action(handle.action_id, token):
                    if not self.store.renew_lease(
                        task_id,
                        owner_id=self.owner_id,
                        ttl_seconds=self.lease_ttl_seconds,
                    ):
                        await self.bridge.cancel_action(handle.action_id)
                        return WorkerResult(
                            task=self._require(task_id),
                            stages=tuple(stages + ["lease_lost"]),
                        )
                    terminal = event
                fresh = self._require(task_id)
                if fresh.cancel_requested or token.requested():
                    task = fresh
                    task = self._set_status(task, TaskStatus.CANCELLED, "user_cancelled")
                    break
                task = self._set_active_action(task, None)
                after = await self.bridge.observe()
                self._checkpoint(task, after)
                world = world.merge(after)
                if terminal is None:
                    task = self._set_status(task, TaskStatus.FAILED, "action_no_result")
                    break
                evaluation = self.evaluator.evaluate(
                    step=step,
                    before=before,
                    result=terminal,
                    after=after,
                    task=task,
                    retry_count=0,
                    retry_limit=task.budget.retries_per_error,
                )
                task = self._record_count(task, after.item_count(task.goal.resource))
                if task.status is TaskStatus.SUCCEEDED:
                    break
                gate_decision = gate.decide(
                    event_id=terminal.event_id,
                    event_type="step_result",
                    task=task,
                    plan=plan,
                    evaluation=evaluation,
                    current_plan_version=plan.plan_version,
                    strategy=plan.strategy,
                )
                self._emit(
                    task,
                    "REASONING_GATE_DECISION",
                    gate_decision.model_dump(mode="json"),
                )
                if evaluation.result is LocalEvaluationType.LOCAL_RECOVERY:
                    task = self._set_status(task, TaskStatus.RECOVERING, evaluation.reason_code)
                    task = self._set_status(task, TaskStatus.EXECUTING, "local_recovery_complete")
                elif evaluation.result in {
                    LocalEvaluationType.ESCALATE,
                    LocalEvaluationType.BLOCKED,
                    LocalEvaluationType.FAILED,
                }:
                    task = self._set_status(
                        task,
                        TaskStatus.BLOCKED,
                        gate_decision.reason_code,
                    )
                    break
                stages.append(step.step_id)
            if not task.status.terminal:
                final = await self.bridge.observe()
                task = self._record_count(task, final.item_count(task.goal.resource))
                if not task.status.terminal:
                    if _rolling_cycle + 1 < task.budget.action_count:
                        continued = await self.run_cognitive(
                            task_id,
                            _lease_acquired=True,
                            _rolling_cycle=_rolling_cycle + 1,
                            _actions_used=_actions_used,
                        )
                        return WorkerResult(
                            task=continued.task,
                            stages=tuple(stages) + continued.stages,
                        )
                    task = self._set_status(
                        task, TaskStatus.BLOCKED, "action_budget_exhausted"
                    )
            return WorkerResult(task=task, stages=tuple(stages))
        finally:
            if task.status.terminal:
                self.cancellations.release(task.cancellation_scope)
                self._task_reasoning_gates.pop(task_id, None)
            if not _lease_acquired:
                self.store.release_lease(task_id, owner_id=self.owner_id)

    def _record_count(self, task: MinecraftTask, count: int) -> MinecraftTask:
        previous_version = task.version
        updated = with_observation_count(task, count)
        updated = self.store.update(updated, expected_version=previous_version)
        self._emit(
            updated,
            "INVENTORY_OBSERVED",
            {"current_count": count, "net_acquired": updated.net_acquired},
        )
        if updated.status is TaskStatus.SUCCEEDED:
            self._emit(updated, "TASK_COMPLETED", {"net_acquired": updated.net_acquired})
        return updated

    def _set_status(
        self,
        task: MinecraftTask,
        status: TaskStatus,
        reason: str,
    ) -> MinecraftTask:
        previous_version = task.version
        updated = transition(
            task,
            status,
            error_code=None if status is TaskStatus.SUCCEEDED else reason,
        )
        updated = self.store.update(updated, expected_version=previous_version)
        self._emit(updated, f"TASK_{status.value.upper()}", {"reason": reason})
        return updated

    def _set_active_action(
        self,
        task: MinecraftTask,
        action_id: str | None,
    ) -> MinecraftTask:
        previous_version = task.version
        updated = task.model_copy(
            update={
                "active_action_id": action_id,
                "version": task.version + 1,
            }
        )
        return self.store.update(updated, expected_version=previous_version)

    def _set_plan_version(self, task: MinecraftTask, plan_version: int) -> MinecraftTask:
        previous_version = task.version
        updated = task.model_copy(
            update={
                "plan_version": int(plan_version),
                "version": task.version + 1,
            }
        )
        return self.store.update(updated, expected_version=previous_version)

    def _emit(self, task: MinecraftTask, event_type: str, payload: dict) -> None:
        event = MinecraftTaskEvent(
            task_id=task.task_id,
            event_type=event_type,
            payload=payload,
        )
        self.store.append_event(event)
        self.trace.emit(event)
        self.progress.publish(task, event)

    def _checkpoint(self, task: MinecraftTask, observation) -> None:
        self.store.save_checkpoint(
            MinecraftCheckpoint(
                task_id=task.task_id,
                version=task.version,
                plan_version=task.plan_version,
                payload={
                    "bot_id": observation.bot_id,
                    "server_id": observation.server_id,
                    "world_id": observation.world_id,
                    "baseline_count": task.baseline_count,
                    "current_count": observation.item_count(task.goal.resource),
                    "active_action_id": task.active_action_id,
                },
            )
        )

    def _require(self, task_id: str) -> MinecraftTask:
        task = self.store.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task
