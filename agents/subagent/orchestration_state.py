from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from config import SUBAGENT_MAX_FAILURES_PER_CLUE, SUBAGENT_MAX_FANOUTS_PER_RUN


ORCHESTRATION_STATE_KEY = "_subagent_orchestration_state"
REJECTION_REASON = "subagent_orchestration_rejected"
FANOUT_BUDGET_REASON = "subagent_fanout_budget_exceeded"

STAGE_SUBAGENT = "subagent"
STAGE_NARROWED = "narrowed_subagent"
STAGE_TEAMMATE = "teammate"
STAGE_SELF = "self"
STAGE_TERMINAL = "terminal"


@dataclass(frozen=True)
class OrchestrationDecision:
    allowed: bool
    reason: str = ""
    message: str = ""
    retry_hint: str = ""
    clue_key: str = ""
    state: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "success": False,
            "status": "rejected",
            "failure_reason": self.reason,
            "failure_message": self.message,
            "recoverable": True,
            "retry_hint": self.retry_hint,
            "clue_key": self.clue_key,
            "state": self.state or {},
        }
        state = self.state or {}
        for key in (
            "dispatch_rejected",
            "missing_paths",
            "directory_paths",
            "suggestions",
            "hint",
        ):
            if key in state:
                payload[key] = state[key]
        return payload


def guard_subagent_dispatch(
    session,
    tasks: list[dict[str, Any]],
    *,
    tool_name: str,
) -> OrchestrationDecision:
    state = _state(session)
    if state["fanout_count"] >= SUBAGENT_MAX_FANOUTS_PER_RUN:
        decision = OrchestrationDecision(
            allowed=False,
            reason=FANOUT_BUDGET_REASON,
            message=(
                "Subagent fan-out budget is exhausted for this run. "
                f"fanout_count={state['fanout_count']} limit={SUBAGENT_MAX_FANOUTS_PER_RUN}."
            ),
            retry_hint=(
                "Do not call parallel_tasks/task again in this run. Use spawn_teammate, "
                "direct parent handling, or report the clue as incomplete with reason."
            ),
            state={"fanout_count": state["fanout_count"], "tool_name": tool_name},
        )
        _record_rejection(session, decision)
        return decision

    for task in tasks:
        clue_key = clue_key_for_task(task)
        clue = _clue_state(state, clue_key)
        if int(clue.get("subagent_failures") or 0) >= SUBAGENT_MAX_FAILURES_PER_CLUE:
            decision = OrchestrationDecision(
                allowed=False,
                reason=REJECTION_REASON,
                message=(
                    f"Subagent already failed {clue.get('subagent_failures')} times for this clue. "
                    "The degradation ladder now requires spawn_teammate or direct parent handling. "
                    "Further subagent fan-out is forbidden for this clue."
                ),
                retry_hint=(
                    "Use spawn_teammate for synthesis, handle the clue directly with a small verified "
                    "scope, or report it as incomplete. Do not dispatch another subagent for this clue."
                ),
                clue_key=clue_key,
                state=clue,
            )
            _record_rejection(session, decision)
            return decision
        if str(clue.get("stage") or STAGE_SUBAGENT) in {STAGE_TEAMMATE, STAGE_SELF, STAGE_TERMINAL}:
            decision = OrchestrationDecision(
                allowed=False,
                reason=REJECTION_REASON,
                message=(
                    f"Clue stage is {clue.get('stage')}; subagent dispatch would move backward "
                    "in the degradation ladder."
                ),
                retry_hint=(
                    "Continue forward in the ladder: spawn_teammate, direct parent handling, "
                    "or honest incomplete reporting."
                ),
                clue_key=clue_key,
                state=clue,
            )
            _record_rejection(session, decision)
            return decision
    return OrchestrationDecision(allowed=True)


def record_subagent_dispatch(session, tasks: list[dict[str, Any]], *, tool_name: str) -> None:
    state = _state(session)
    state["fanout_count"] = int(state.get("fanout_count") or 0) + 1
    for task in tasks:
        clue_key = clue_key_for_task(task)
        clue = _clue_state(state, clue_key)
        clue["dispatch_attempts"] = int(clue.get("dispatch_attempts") or 0) + 1
        clue["last_tool"] = tool_name
    _save_state(session, state)


def record_subagent_results(
    session,
    tasks: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    state = _state(session)
    for task, result in zip(tasks, results):
        clue_key = clue_key_for_task(task)
        clue = _clue_state(state, clue_key)
        if _result_failed(result):
            clue["subagent_failures"] = int(clue.get("subagent_failures") or 0) + 1
            reasons = clue.setdefault("failure_reasons", [])
            reason = str(result.get("failure_reason") or result.get("stop_reason") or "unknown")
            if isinstance(reasons, list):
                reasons.append(reason)
                clue["failure_reasons"] = reasons[-8:]
            clue["stage"] = _next_stage_after_failure(int(clue["subagent_failures"]))
        else:
            clue["stage"] = STAGE_TERMINAL
            clue["completed"] = True
    _save_state(session, state)


def record_subagent_rejection(session, decision: OrchestrationDecision) -> None:
    _record_rejection(session, decision)


def clue_key_for_task(task: dict[str, Any]) -> str:
    description = _squash(task.get("description") or task.get("objective") or "", 160)
    files = _scope_files(task.get("scope"))
    if files:
        basis = {"description": description, "files": files}
    else:
        basis = {
            "description": description,
            "prompt": _squash(task.get("prompt") or "", 240),
        }
    rendered = json.dumps(basis, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha1(rendered.encode("utf-8")).hexdigest()[:12]
    return f"clue:{digest}"


def rejection_response(decision: OrchestrationDecision) -> str:
    return json.dumps(decision.to_payload(), ensure_ascii=False, indent=2)


def rejected_parallel_response(decision: OrchestrationDecision) -> str:
    payload = decision.to_payload()
    payload["results"] = []
    return json.dumps(payload, ensure_ascii=False, indent=2)


def rejection_trace_payload(decision: OrchestrationDecision, *, tool_name: str) -> dict[str, Any]:
    payload = decision.to_payload()
    payload["tool_name"] = tool_name
    return payload


def _state(session) -> dict[str, Any]:
    metadata = getattr(session, "metadata", None)
    if not isinstance(metadata, dict):
        return {"fanout_count": 0, "fanout_rejected_count": 0, "clues": {}}
    state = metadata.get(ORCHESTRATION_STATE_KEY)
    if not isinstance(state, dict):
        state = {"fanout_count": 0, "fanout_rejected_count": 0, "clues": {}}
        metadata[ORCHESTRATION_STATE_KEY] = state
    if not isinstance(state.get("clues"), dict):
        state["clues"] = {}
    state["fanout_count"] = int(state.get("fanout_count") or 0)
    state["fanout_rejected_count"] = int(state.get("fanout_rejected_count") or 0)
    return state


def _save_state(session, state: dict[str, Any]) -> None:
    metadata = getattr(session, "metadata", None)
    if isinstance(metadata, dict):
        metadata[ORCHESTRATION_STATE_KEY] = state


def _clue_state(state: dict[str, Any], clue_key: str) -> dict[str, Any]:
    clues = state.setdefault("clues", {})
    clue = clues.setdefault(
        clue_key,
        {
            "dispatch_attempts": 0,
            "subagent_failures": 0,
            "stage": STAGE_SUBAGENT,
            "failure_reasons": [],
        },
    )
    return clue


def _record_rejection(session, decision: OrchestrationDecision) -> None:
    state = _state(session)
    state["fanout_rejected_count"] = int(state.get("fanout_rejected_count") or 0) + 1
    if decision.clue_key:
        clue = _clue_state(state, decision.clue_key)
        clue["last_rejection_reason"] = decision.reason
    _save_state(session, state)


def _result_failed(result: dict[str, Any]) -> bool:
    return (
        not bool(result.get("success"))
        or bool(result.get("incomplete"))
        or bool(result.get("failure_reason"))
    )


def _next_stage_after_failure(failures: int) -> str:
    if failures <= 0:
        return STAGE_SUBAGENT
    if failures == 1:
        return STAGE_NARROWED
    return STAGE_TEAMMATE


def _scope_files(scope: Any) -> list[str]:
    if not isinstance(scope, dict):
        return []
    files = scope.get("files")
    if not isinstance(files, list):
        return []
    return sorted(str(item) for item in files if str(item or "").strip())


def _squash(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if len(text) <= limit:
        return text
    return text[:limit]
