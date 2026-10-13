"""TaskState prompt rendering and event-window compaction for coding sessions.

`CodingContextSnapshot` is a read-only projection. All mutable task facts
live in :mod:`applications.coding.task_state`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import re
from html import escape
import time
from typing import Any, Sequence

from config import (
    CONTEXT_PRESSURE_WINDOW_TOKENS,
    PROMPT_COMPACTION_TARGET_RATIO,
    PROMPT_SOFT_COMPACTION_RATIO,
)
from runtime.context.dynamic_budget import PromptBudgetExceeded, select_complete_groups
from runtime.context.events import ContextEvent, ContextEventType, is_real_user_message, thaw
from runtime.context.snapshots import (
    ContextSnapshot,
    ContextSnapshotManager,
    EventCompactor,
    active_context_snapshot,
)
from runtime.token_estimator import estimate_tokens
from runtime.workspace import workspace_root_for_session

from .evidence_validity import refresh_evidence_validity

from .task_state import (
    Objective,
    TaskState,
    ensure_task_state,
    load_task_state,
    save_task_state,
)


CODING_CONTEXT_SNAPSHOT_VERSION = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MessageGroup:
    start: int
    end: int
    messages: list[dict[str, Any]]
    kind: str
    closed: bool = True


@dataclass(frozen=True)
class CodingContextSnapshot:
    """Small renderer/checkpoint metadata, never a second task state."""

    version: int = CODING_CONTEXT_SNAPSHOT_VERSION
    task_id: str = ""
    task_state_version: int = 1
    generation: int = 0
    compacted_until_event_id: str = ""
    source_event_start_id: str = ""
    source_event_end_id: str = ""
    prompt_tail_start_index: int = 0
    compacted_until_index: int = 0
    last_compaction: dict[str, Any] | None = None
    summary: str = ""
    source_requests: tuple[dict[str, Any], ...] = ()
    historical_requests: tuple[dict[str, Any], ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=_now)

    @classmethod
    def from_payload(cls, payload: Any) -> "CodingContextSnapshot | None":
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return None
        if not isinstance(payload, dict) or int(payload.get("version") or 0) < 2:
            return None
        fields = cls.__dataclass_fields__
        return cls(**{key: value for key, value in payload.items() if key in fields})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CodingContextView:
    state: CodingContextSnapshot
    task_state: TaskState
    state_message: dict[str, Any]
    recent_messages: list[dict[str, Any]]
    active_messages: list[dict[str, Any]]
    compacted: bool
    before_tokens: int
    after_tokens: int
    reduction: dict[str, Any] | None


def load_coding_context_snapshot(session) -> CodingContextSnapshot | None:
    active = active_context_snapshot(session)
    if active is None:
        return None
    return CodingContextSnapshot(
        task_id=str(getattr(session, "id", "") or ""),
        task_state_version=active.source_task_state_version,
        generation=active.generation,
        compacted_until_event_id=active.covered_event_end_id,
        source_event_start_id=active.covered_event_start_id,
        source_event_end_id=active.covered_event_end_id,
        prompt_tail_start_index=_message_tail_after_archive(session),
        compacted_until_index=active.covered_event_end_seq,
        last_compaction=active.to_dict(),
        summary=active.summary,
    )


def build_coding_context_view(
    session,
    *,
    objective: str,
    active_turn_start_index: int | None,
    static_messages: list[dict[str, Any]],
    usable_input_tokens: int | None = None,
    event_compactor: EventCompactor | None = None,
) -> CodingContextView:
    """Build one ephemeral prompt view from TaskState and the active event tail.

    Threshold and target values are derived from the active model budget.
    """

    backfill = getattr(session, "_backfill_legacy_messages", None)
    if callable(backfill):
        backfill()
    from runtime.context.policies import context_policy, plan_context

    policy = context_policy(session)
    messages = [dict(item) for item in (getattr(session, "messages", []) or [])]
    start = max(0, min(int(active_turn_start_index or 0), len(messages)))
    previous_snapshot = load_coding_context_snapshot(session)
    tail_start = max(
        start,
        min(
            int(previous_snapshot.prompt_tail_start_index or 0)
            if previous_snapshot is not None
            else 0,
            len(messages),
        ),
    )
    if policy.name != "current":
        # Experimental views always replan from immutable full history. Old
        # archive boundaries cannot prevent evidence from being restored.
        tail_start = 0
    pinned_user = _latest_real_user_message(messages)
    active_messages = _pin_latest_user(messages[tail_start:], pinned_user)
    event_ref = _latest_real_user_event_ref(session)
    artifact_refs = _latest_user_artifact_refs(messages)
    task_state = ensure_task_state(
        session,
        objective_summary=_objective_summary(objective),
        original_request_ref=event_ref,
        artifact_refs=artifact_refs,
    )
    task_state = _apply_user_objective_change(
        session,
        task_state,
        objective=objective,
        event_ref=event_ref,
        artifact_refs=artifact_refs,
    )
    task_state = refresh_evidence_validity(task_state, workspace_root_for_session(session))
    snapshot = _snapshot(session, task_state, prompt_tail_start_index=tail_start)
    state_message = render_coding_context_message(task_state, snapshot=snapshot)
    groups = group_messages(messages, start_index=tail_start)
    recent_messages = _pin_latest_user(_flatten(groups), pinned_user)
    before_tokens = estimate_tokens([*static_messages, state_message, *recent_messages])

    if usable_input_tokens is None:
        available = max(
            1,
            CONTEXT_PRESSURE_WINDOW_TOKENS - estimate_tokens(static_messages),
        )
    else:
        dynamic_static = [
            message
            for message in static_messages
            if str(message.get("role") or "") != "system"
        ]
        available = max(
            1,
            int(usable_input_tokens) - estimate_tokens(dynamic_static),
        )
    configured = isinstance(session.metadata.get("context_policy"), dict)
    soft_ratio = policy.soft_ratio if configured else PROMPT_SOFT_COMPACTION_RATIO
    target_ratio = policy.target_ratio if configured else PROMPT_COMPACTION_TARGET_RATIO
    soft_trigger = max(1, int(available * soft_ratio))
    target = max(1, int(available * target_ratio))
    request = session.metadata.get("context_compaction_request") or {}
    proactive = request.get("status") == "pending" and all(group.closed for group in groups)
    if policy.name in {"masking", "adaptive", "raw", "native"}:
        from .reasoning_memory import reasoning_context_view
        focus = {"reasoning": reasoning_context_view(task_state), "request": request.get("focus", "")}
        plan = plan_context(session, groups=groups, policy=policy,
                            max_tokens=max(1, target - estimate_tokens([state_message])), focus=focus)
        recent_messages = _pin_latest_user(plan.messages, pinned_user)
        session.metadata["context_policy_audit"] = plan.audit()
    else:
        session.metadata["context_policy_audit"] = {"version": 1, "policy": "current"}
    compacted = False
    reduction: dict[str, Any] | None = None
    compaction_duration_ms = 0

    # Fixed sections have already been reserved in available. Compare only the
    # managed state/tail here, rather than charging system/history a second time.
    managed_tokens = estimate_tokens([state_message, *recent_messages])
    if policy.name == "current" and (managed_tokens > soft_trigger or proactive) and len(groups) > 1:
        flattened_groups = _flatten(groups)
        pinned_tokens = (
            estimate_tokens([pinned_user])
            if pinned_user is not None and pinned_user not in flattened_groups
            else 0
        )
        selected = _select_recent_groups(
            groups,
            max_tokens=max(
                1,
                target - estimate_tokens([state_message]) - pinned_tokens,
            ),
        )
        kept_start = _first_selected_index(groups, selected)
        if kept_start > tail_start:
            compaction_started = time.perf_counter()
            result = _compact_events(
                session,
                task_state=task_state,
                before_message_index=kept_start,
                event_compactor=event_compactor,
            )
            compaction_duration_ms = max(
                0,
                int((time.perf_counter() - compaction_started) * 1000),
            )
            if result is not None:
                task_state, checkpoint = result
                task_state = refresh_evidence_validity(task_state, workspace_root_for_session(session))
                recent_messages = _pin_latest_user(selected, pinned_user)
                snapshot = _snapshot(
                    session,
                    task_state,
                    prompt_tail_start_index=kept_start,
                    checkpoint=checkpoint,
                )
                state_message = render_coding_context_message(
                    task_state, snapshot=snapshot
                )
                compacted = True
                reduction = {
                    "section": "coding_context",
                    "reason": "task_state_semantic_compaction",
                    "before_tokens": before_tokens,
                    "after_tokens": estimate_tokens([
                        *static_messages, state_message, *recent_messages
                    ]),
                    "soft_trigger_tokens": soft_trigger,
                    "target_tokens": target,
                    "generation": checkpoint.generation,
                    "compacted_until_event_id": checkpoint.covered_event_end_id,
                }

    if proactive and policy.name != "native":
        session.metadata["context_compaction_request"] = {
            **request, "status": "completed" if compacted or policy.name in {"masking", "adaptive"} else "no_change",
            "handled_by": policy.name, "generation": snapshot.generation,
        }
    after_tokens = estimate_tokens([*static_messages, state_message, *recent_messages])
    runtime_metrics = (getattr(session, "metadata", {}) or {}).get("context_metrics")
    metrics = {
        **dict(snapshot.metrics or {}),
        **(dict(runtime_metrics) if isinstance(runtime_metrics, dict) else {}),
        "prompt_tokens_before_compaction": before_tokens,
        "prompt_tokens_after_compaction": after_tokens,
        "task_state_tokens": estimate_tokens([state_message]),
        "recent_tail_tokens": estimate_tokens(recent_messages),
        "compaction_generation": snapshot.generation,
        "compacted_event_count": max(
            0,
            int((snapshot.last_compaction or {}).get("covered_event_end_seq") or 0)
            - int((snapshot.last_compaction or {}).get("covered_event_start_seq") or 0)
            + (1 if snapshot.last_compaction else 0),
        ),
        "compaction_duration_ms": compaction_duration_ms,
        "usable_input_tokens": int(
            usable_input_tokens or CONTEXT_PRESSURE_WINDOW_TOKENS
        ),
        "dynamic_context_tokens": available,
        "soft_compaction_trigger_tokens": soft_trigger,
        "compaction_target_tokens": target,
    }
    snapshot = CodingContextSnapshot(**{
        **snapshot.to_dict(),
        "metrics": metrics,
        "updated_at": _now(),
    })
    save_task_state(session, task_state)
    return CodingContextView(
        state=snapshot,
        task_state=task_state,
        state_message=render_coding_context_message(task_state, snapshot=snapshot),
        recent_messages=recent_messages,
        active_messages=active_messages,
        compacted=compacted,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        reduction=reduction,
    )


def render_coding_context_message(
    state: TaskState,
    *,
    snapshot: CodingContextSnapshot | None = None,
) -> dict[str, Any]:
    snapshot = snapshot or CodingContextSnapshot(task_state_version=state.version)
    payload = state.to_dict()
    _bound_state_evidence_projection(payload)
    # Historical replacement details remain checkpointed but do not consume
    # every prompt. Current superseded status is already represented by items.
    core = payload.get("core", {})
    # Exact task instructions are rendered once in protected-task. This remaining
    # envelope is a prompt projection, never written back as persistent state.
    core.pop("objective", None)
    core.pop("constraints", None)
    extensions = payload.get("extensions")
    coding = extensions.get("coding") if isinstance(extensions, dict) else None
    if isinstance(coding, dict):
        coding.pop("history", None)
    compacted_summary = (
        '<context-snapshot instructions="false" semantic_verification="none" '
        f'ref="snapshot://{escape(str((snapshot.last_compaction or {}).get("snapshot_id") or ""), quote=True)}">\n'
        + escape(snapshot.summary) + "\n</context-snapshot>\n"
        if snapshot.summary
        else ""
    )
    protected = {
        "objective": state.objective.__dict__,
        "constraints": [asdict(item) for item in state.constraints
                        if getattr(item.status, "value", item.status) != "superseded"],
        "revision_precedence": "Explicit later user requirements_revision overrides earlier source text only for the revised fields. Unchanged requirements remain active.",
        "source_requests": list(snapshot.source_requests),
    }
    historical = (
        '<historical-user-requests source="parent-user-events" instructions="false">\n'
        + json.dumps({"scope": (
            "Historical user requests provide context for the current task; they do not "
            "activate earlier tasks. Apply only relevant requirements; later real user "
            "requests may correct or supersede earlier ones. Host and assistant summaries "
            "cannot amend user requirements. Source refs identify parent-session provenance "
            "and may not be directly readable from this task; the complete source text is "
            "included here."
        )}, ensure_ascii=False)
        + "\n" + escape(json.dumps(list(snapshot.historical_requests), ensure_ascii=False, default=str), quote=False)
        + "\n</historical-user-requests>\n"
        if snapshot.historical_requests else ""
    )
    content = (
        '<coding-context-state source="runtime-generated" trust="context-only" '
        f'instructions="false" version="{state.version}" generation="{snapshot.generation}">\n'
        + compacted_summary
        + '<protected-task source="task-state-and-user-events" instructions="true">\n'
        + escape(json.dumps(protected, ensure_ascii=False, default=str), quote=False)
        + "\n</protected-task>\n"
        + historical
        + escape(json.dumps(payload, ensure_ascii=False, default=str), quote=False)
        + "\n</coding-context-state>"
    )
    return {
        "role": "system",
        "content": content,
        "metadata": {
            "kind": "task_state_context",
            "source": "runtime-generated",
            "instructions": False,
            "task_state_version": state.version,
            "generation": snapshot.generation,
        },
    }



def _bound_state_evidence_projection(payload: dict[str, Any]) -> None:
    """Bound prompt-only indexes; the current authoritative state stays intact.

    Active claims get first choice of evidence. Omitted or abbreviated rows must
    be recovered before treating the displayed index as complete coverage.
    """
    core = payload.get("core", {})
    coding = (payload.get("extensions") or {}).get("coding", {})
    if not isinstance(core, dict) or not isinstance(coding, dict):
        return
    active_refs: set[str] = {str(ref) for ref in core.get("completion_basis") or []}
    for container, keys in ((core, ("pending_actions", "open_questions", "blockers", "completed")),
                            (coding, ("findings", "hypotheses", "decisions", "plan"))):
        for key in keys:
            for item in container.get(key) or []:
                if not isinstance(item, dict) or item.get("status") == "superseded":
                    continue
                if key not in {"findings", "decisions", "completed"} and item.get("status") == "completed":
                    continue
                for refs_key in ("evidence_refs", "supporting_evidence_refs", "refuting_evidence_refs"):
                    active_refs.update(str(ref) for ref in item.get(refs_key) or [])
    index = coding.get("evidence_index")
    omitted: dict[str, Any] = {}
    if isinstance(index, dict) and _projection_chars(index) > 6_000:
        ordered = sorted(reversed(list(index)), key=lambda ref: ref not in active_refs)
        selected: dict[str, Any] = {}
        remaining = 6_000
        abbreviated = 0
        for ref in ordered:
            row = index[ref]
            cost = _projection_chars({ref: row})
            if cost > remaining and ref in active_refs and isinstance(row, dict):
                row = {key: row[key] for key in (
                    "id", "event_id", "kind", "artifact_ref", "tool_result_ref", "path",
                    "lines", "content_hash", "stale", "stale_reason", "verification", "tool_name", "effect_state", "provenance", "task_id",
                ) if key in row}
                row["details_omitted"] = True
                row["recovery_ref"] = "state://current"
                cost = _projection_chars({ref: row})
            if cost <= remaining:
                selected[ref] = row
                remaining -= cost
                abbreviated += bool(isinstance(row, dict) and row.get("details_omitted"))
        coding["evidence_index"] = selected
        omitted["evidence_index"] = {
            "total": len(index), "shown": len(selected), "abbreviated": abbreviated,
            "active_references_omitted": len((active_refs & index.keys()) - selected.keys()),
        }
    coverage = coding.get("coverage")
    if isinstance(coverage, dict) and _projection_chars(coverage) > 2_500:
        projected: dict[str, Any] = {}
        counts: dict[str, int] = {}
        remaining = 2_200
        # Open coverage gaps and entries backing live claims precede routine
        # lists of previously inspected files and tests.
        keys = sorted(coverage, key=lambda key: (key not in {"uncovered", "areas_unchecked"}, key != "entries"))
        for key in keys:
            values = coverage[key]
            if not isinstance(values, list):
                continue
            entries = list(reversed(values))
            if key == "entries":
                entries.sort(key=lambda row: not (
                    isinstance(row, dict) and active_refs.intersection(row.get("evidence_refs") or [])
                ))
            kept = []
            for row in entries:
                cost = _projection_chars(row) + 2
                if cost <= remaining:
                    kept.append(row)
                    remaining -= cost
            projected[key] = kept
            if len(kept) < len(values):
                counts[key] = len(values) - len(kept)
        projected["projection_incomplete"] = True
        coding["coverage"] = projected
        omitted["coverage"] = counts
    artifacts = core.get("artifact_refs")
    if isinstance(artifacts, list) and _projection_chars(artifacts) > 1_000:
        kept, remaining = [], 950
        for ref in reversed(artifacts):
            cost = _projection_chars(ref) + 2
            if cost <= remaining:
                kept.append(ref)
                remaining -= cost
        core["artifact_refs"] = list(reversed(kept))
        omitted["artifact_refs"] = len(artifacts) - len(kept)
    if omitted:
        payload["evidence_projection"] = {
            "partial": True, "omitted": omitted, "source_version": core.get("version"),
            "recovery_ref": "state://current",
            "recovery": "Use read_artifact(artifact_ref='state://current', query='reference or field') or offset/limit. This is current mutable state: restart pagination if version or state_sha256 changes. Omitted evidence and coverage must not be interpreted as absent, verified, or complete.",
        }


def _projection_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))

def group_messages(
    messages: list[dict[str, Any]], *, start_index: int = 0
) -> list[MessageGroup]:
    """Group complete user turns and assistant/tool transactions."""
    groups: list[MessageGroup] = []
    index = max(0, int(start_index or 0))
    while index < len(messages):
        begin = index
        first = messages[index] if isinstance(messages[index], dict) else {}
        role = str(first.get("role") or "")
        if role == "user":
            index += 1
            if index < len(messages):
                candidate = messages[index]
                if (
                    isinstance(candidate, dict)
                    and str(candidate.get("role") or "") == "assistant"
                    and not candidate.get("tool_calls")
                ):
                    index += 1
            chunk = [dict(item) for item in messages[begin:index] if isinstance(item, dict)]
            groups.append(MessageGroup(begin, index, chunk, "turn", _group_closed(chunk)))
            continue
        if role == "assistant" and first.get("tool_calls"):
            expected = _tool_call_ids(first.get("tool_calls") or [])
            index += 1
            seen: set[str] = set()
            while index < len(messages):
                candidate = messages[index]
                if not isinstance(candidate, dict) or str(candidate.get("role") or "") != "tool":
                    break
                seen.add(str(candidate.get("tool_call_id") or ""))
                index += 1
                if expected and expected <= seen:
                    break
            chunk = [dict(item) for item in messages[begin:index] if isinstance(item, dict)]
            groups.append(MessageGroup(begin, index, chunk, "tool_transaction", not expected or expected <= seen))
            continue
        index += 1
        groups.append(MessageGroup(begin, index, [dict(first)], role or "message"))
    return groups


def _compact_events(
    session,
    *,
    task_state: TaskState,
    before_message_index: int,
    event_compactor: EventCompactor | None,
) -> tuple[TaskState, ContextSnapshot] | None:
    events = _events_before_message_index(session, before_message_index)
    if not events:
        return None
    compactor = event_compactor or EventCompactor()
    event_payloads = [event.to_dict() for event in events]
    manager = ContextSnapshotManager(
        prepare_writer=getattr(session, "_context_snapshot_prepare", None),
        activation_writer=getattr(session, "_context_snapshot_activate", None),
        archive_writer=getattr(session, "_context_snapshot_archive", None),
    )
    try:
        output = compactor.compact(
            task_state=task_state,
            events=event_payloads,
            covered_start_seq=events[0].seq,
            covered_end_seq=events[-1].seq,
            previous_snapshot=active_context_snapshot(session),
        )
        prepared = manager.prepare(
            session,
            output=output,
            events=events,
            source_task_state_version=task_state.version,
            evidence_refs=list(getattr(task_state, "evidence_index", {}) or {}),
            artifact_refs=list(getattr(task_state, "artifact_refs", []) or []),
        )
        active = manager.activate(session, prepared.snapshot_id)
    except Exception as exc:
        _record_compaction_failure(session, exc)
        return None
    return task_state, active


def _snapshot(
    session,
    task_state: TaskState,
    *,
    prompt_tail_start_index: int = 0,
    checkpoint: ContextSnapshot | None = None,
) -> CodingContextSnapshot:
    latest_checkpoint = checkpoint or active_context_snapshot(session)
    generation = latest_checkpoint.generation if latest_checkpoint else 0
    boundary = latest_checkpoint.covered_event_end_id if latest_checkpoint else ""
    return CodingContextSnapshot(
        task_id=str(getattr(session, "id", "") or ""),
        task_state_version=task_state.version,
        generation=generation,
        compacted_until_event_id=boundary,
        source_event_start_id=(latest_checkpoint.covered_event_start_id if latest_checkpoint else ""),
        source_event_end_id=(latest_checkpoint.covered_event_end_id if latest_checkpoint else ""),
        prompt_tail_start_index=prompt_tail_start_index,
        compacted_until_index=int(getattr(session, "archive_boundary_seq", 0) or 0),
        last_compaction=(latest_checkpoint.to_dict() if latest_checkpoint else None),
        summary=(latest_checkpoint.summary if latest_checkpoint else ""),
        source_requests=_source_requests(session, task_state),
        historical_requests=tuple(
            (getattr(session, "metadata", {}) or {}).get("coding_session_handoff", {}).get("source_requests", [])
        ),
    )


def _select_recent_groups(groups: list[MessageGroup], *, max_tokens: int) -> list[dict[str, Any]]:
    raw_groups = [group.messages for group in groups]
    required = {len(groups) - 1}
    latest_user_group = _latest_real_user_group(groups)
    if latest_user_group is not None:
        required.add(latest_user_group)
    required.update(index for index, group in enumerate(groups) if not group.closed)
    return select_complete_groups(
        raw_groups,
        max_tokens=max_tokens,
        required_group_indexes=required,
    )


def _first_selected_index(groups: list[MessageGroup], selected: list[dict[str, Any]]) -> int:
    if not selected:
        return groups[-1].start if groups else 0
    selected_firsts = [
        message
        for message in selected
        if isinstance(message, dict)
    ]
    suffix_start = len(groups) - 1
    for index in range(len(groups) - 1, -1, -1):
        first = groups[index].messages[0] if groups[index].messages else None
        if first is None or first not in selected_firsts:
            break
        suffix_start = index
    return groups[suffix_start].start if groups else 0


def _events_before_message_index(session, index: int) -> list[ContextEvent]:
    result = []
    archive_boundary = int(getattr(session, "archive_boundary_seq", 0) or 0)
    active_events = getattr(session, "active_event_window", None)
    events = (
        list(active_events)
        if isinstance(active_events, list)
        else [
            event
            for event in list(getattr(session, "event_log", []) or [])
            if event.seq > archive_boundary
        ]
    )
    for event in events:
        payload = thaw(event.payload)
        message_index = payload.get("legacy_message_index")
        message = payload.get("message")
        if message_index is None and isinstance(message, dict):
            message_index = _matching_message_index(session, message)
        if message_index is not None and int(message_index) >= index:
            continue
        result.append(event)
    return result


def _message_tail_after_archive(session) -> int:
    boundary = int(getattr(session, "archive_boundary_seq", 0) or 0)
    indexes = []
    for event in list(getattr(session, "event_log", []) or []):
        if event.seq > boundary:
            break
        payload = thaw(event.payload)
        message_index = payload.get("legacy_message_index")
        message = payload.get("message")
        if message_index is None and isinstance(message, dict):
            message_index = _matching_message_index(session, message)
        if message_index is not None:
            indexes.append(int(message_index))
    return max(indexes, default=-1) + 1


def _matching_message_index(session, target: dict[str, Any]) -> int | None:
    for index, message in enumerate(list(getattr(session, "messages", []) or [])):
        if isinstance(message, dict) and message == target:
            return index
    return None


def _event_seq(session, event_id: str | None) -> int:
    for event in list(getattr(session, "event_log", []) or []):
        if event.event_id == event_id:
            return event.seq
    return int(getattr(session, "archive_boundary_seq", 0) or 0)


def _latest_real_user_event_ref(session) -> str:
    for event in reversed(list(getattr(session, "event_log", []) or [])):
        if event.type in {ContextEventType.USER_MESSAGE.value, ContextEventType.USER_CORRECTION.value}:
            payload = thaw(event.payload)
            message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            if not is_real_user_message({"role": "user", **message}):
                continue
            return f"event://{event.event_id}"
    return ""


def _latest_user_artifact_refs(messages: Sequence[dict[str, Any]]) -> list[str]:
    for message in reversed(messages):
        if not is_real_user_message(message):
            continue
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        refs = list(metadata.get("artifact_refs") or [])
        ref = metadata.get("artifact_ref") or message.get("artifact_ref")
        if ref:
            refs.append(ref)
        result = []
        for ref in refs:
            uri = ref.get("storage_uri") or ref.get("artifact_id") if isinstance(ref, dict) else ref
            if uri and str(uri) not in result:
                result.append(str(uri))
        return result
    return []


def _latest_real_user_message(
    messages: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    for message in reversed(messages):
        if not isinstance(message, dict) or str(message.get("role") or "") != "user":
            continue
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        if not is_real_user_message(message):
            continue
        return dict(message)
    return None


def _pin_latest_user(
    messages: Sequence[dict[str, Any]],
    latest_user: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    rendered = [dict(message) for message in messages if isinstance(message, dict)]
    if latest_user is not None and latest_user not in rendered:
        rendered.insert(0, dict(latest_user))
    return rendered


def _latest_real_user_group(groups: Sequence[MessageGroup]) -> int | None:
    for index in range(len(groups) - 1, -1, -1):
        for message in groups[index].messages:
            if str(message.get("role") or "") != "user":
                continue
            metadata = (
                message.get("metadata")
                if isinstance(message.get("metadata"), dict)
                else {}
            )
            if is_real_user_message(message):
                return index
    return None


def _apply_user_objective_change(
    session,
    state: TaskState,
    *,
    objective: str,
    event_ref: str,
    artifact_refs: list[str],
) -> TaskState:
    if not event_ref or event_ref == state.objective.original_request_ref:
        return state
    message = _message_for_event_ref(session, event_ref)
    if message is None or not is_real_user_message(message):
        return state
    metadata = message.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    # Structured host revisions were already applied transactionally by task_lifecycle.
    if isinstance(metadata.get("requirements_revision"), dict):
        return state
    text = str(message.get("content") or "").strip()
    if not text:
        return state
    explicit = (
        metadata.get("objective_change") is True
        or metadata.get("kind") in {"objective_change", "correction", "user_correction"}
        or bool(re.match(
            r"^(?:更正[：:]|纠正[：:]|(?:请)?(?:把)?(?:目标|任务)(?:改为|改成)|"
            r"(?:请)?改为|(?:请)?只检查|correction\s*:|"
            r"change (?:the )?(?:goal|objective|task) to\b|instead[, :] )",
            text, re.IGNORECASE,
        ))
    )
    if not explicit:
        return state
    # The supplied objective may be a runtime rendering; only this source event
    # can amend the target. Existing hard constraints are left untouched.
    objective = text
    state.history.append(_objective_history(state, objective, event_ref))
    state.objective = Objective(
        summary=_objective_summary(objective),
        original_request_ref=event_ref,
        source_artifacts=artifact_refs,
        supersedes=state.objective.original_request_ref,
    )
    from .task_lifecycle import _invalidate_revision_reasoning
    _invalidate_revision_reasoning(state, "User objective changed")
    state.version += 1
    state.updated_at = _now()
    return save_task_state(session, state)


def _message_for_event_ref(session, event_ref: str) -> dict[str, Any] | None:
    event_id = str(event_ref).removeprefix("event://")
    for event in getattr(session, "event_log", []) or []:
        if event.event_id == event_id:
            if event.type not in {ContextEventType.USER_MESSAGE.value, ContextEventType.USER_CORRECTION.value}:
                return None
            payload = thaw(event.payload)
            message = payload.get("message", payload)
            if isinstance(message, dict):
                result = {"role": "user", **message}
                if "content" not in result and "text" in result:
                    result["content"] = result["text"]
                if event.type == ContextEventType.USER_CORRECTION.value:
                    result["metadata"] = {"kind": "user_correction", **dict(result.get("metadata") or {})}
                return result
    return None


def _source_requests(session, state: TaskState) -> tuple[dict[str, Any], ...]:
    """Pin exact user request text; never summarize task constraints with an LLM.

    Full request content can exceed a model budget. The final request gate must
    then reject the prompt instead of silently dropping a hard instruction.
    """
    # Follow-ups can contain new hard constraints without changing the objective.
    # Pin their exact text as well; never classify away an arbitrary user sentence.
    all_events = session.events_after(0) if hasattr(session, "events_after") else session.event_log
    result = []
    for event in all_events:
        if event.type not in {ContextEventType.USER_MESSAGE.value, ContextEventType.USER_CORRECTION.value}:
            continue
        ref = f"event://{event.event_id}"
        message = _message_for_event_ref(session, ref)
        if message is not None and is_real_user_message(message):
            row = {"source_ref": ref, "content": message.get("content", "")}
            revision = (message.get("metadata") or {}).get("requirements_revision")
            if isinstance(revision, dict):
                row["requirements_revision"] = revision
                row["scope"] = "explicit_field_revision; supersedes earlier source only for these fields"
            result.append(row)

    return tuple(result)


def _objective_history(state: TaskState, objective: str, event_ref: str):
    from .task_state import StateHistoryEntry

    return StateHistoryEntry(
        id=f"objective:{state.version}:{event_ref}",
        category="objective",
        item_id="objective",
        previous=state.objective.__dict__.copy(),
        replacement={
            "summary": _objective_summary(objective),
            "original_request_ref": event_ref,
        },
    )


def _objective_summary(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "Complete the current coding task"
    first = text.split("\n\n", 1)[0].strip()
    return " ".join(first.split())[:300]


def _record_compaction_failure(session, error: Exception) -> None:
    metadata = getattr(session, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
        session.metadata = metadata
    metrics = metadata.get("context_metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    causes = _exception_chain(error)
    key = "context_snapshot_failures"
    metrics[key] = int(metrics.get(key, 0) or 0) + 1
    if any(isinstance(item, PromptBudgetExceeded) for item in causes):
        metrics["hard_budget_blocks"] = int(
            metrics.get("hard_budget_blocks", 0) or 0
        ) + 1
    metadata["context_metrics"] = metrics
    append = getattr(session, "append_event", None)
    if callable(append):
        append(ContextEventType.RUNTIME_ERROR, {
            "error_type": type(error).__name__,
            "message": str(error),
            "operation": "context_compaction",
        })


def _exception_chain(error: BaseException) -> list[BaseException]:
    result: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in result:
        result.append(current)
        current = current.__cause__ or current.__context__
    return result


def _group_closed(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        if str(message.get("role") or "") != "assistant" or not message.get("tool_calls"):
            continue
        expected = _tool_call_ids(message.get("tool_calls") or [])
        seen = {
            str(item.get("tool_call_id") or "")
            for item in messages
            if str(item.get("role") or "") == "tool"
        }
        if expected - seen:
            return False
    return True


def _tool_call_ids(calls: Sequence[Any]) -> set[str]:
    return {
        str(call.get("id"))
        for call in calls
        if isinstance(call, dict) and call.get("id")
    }


def _flatten(groups: Sequence[MessageGroup]) -> list[dict[str, Any]]:
    return [dict(message) for group in groups for message in group.messages]
