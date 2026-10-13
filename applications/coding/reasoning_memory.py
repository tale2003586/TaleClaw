"""Task-scoped model reasoning over immutable host observations.

A confirmed hypothesis is a model's evidence-backed interpretation, never a
host assertion that a tool succeeded. Dependencies denote validity prerequisites
(not merely topic associations), and remain explicit in the durable TaskState.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from typing import Any

from .task_state import Hypothesis, ItemStatus, StateHistoryEntry, TaskState

_GROUPS = {
    "finding": ("findings", "findings"),
    "hypothesis": ("hypotheses", "hypotheses"),
    "decision": ("decisions", "decisions"),
    "action": ("pending_actions", "pending_actions"),
    "plan": ("plan", "plan_items"),
    "completed": ("completed", "completed"),
}
LIFECYCLES = {"open", "excluded", "confirmed", "stale"}


def reasoning_nodes(state: TaskState) -> dict[str, Any]:
    return {f"{kind}:{item.id}": item for kind, (attribute, _) in _GROUPS.items()
            for item in getattr(state, attribute)}


def changed_reasoning_nodes(before: TaskState, after: TaskState) -> set[str]:
    previous = reasoning_nodes(before)
    changed = set()
    for key, item in reasoning_nodes(after).items():
        if key not in previous:
            continue
        old, new = asdict(previous[key]), asdict(item)
        for name in ("supersedes", "task_id", "revalidation_required", "invalidation_reason"):
            old.pop(name, None)
            new.pop(name, None)
        if old != new:
            changed.add(key)
    return changed


def evidence_refs_for(item: Any) -> list[str]:
    refs = list(getattr(item, "evidence_refs", []))
    if isinstance(item, Hypothesis):
        refs += item.supporting_evidence_refs + item.refuting_evidence_refs
    return list(dict.fromkeys(refs))


def dependencies_for(key: str, item: Any) -> list[str]:
    refs = list(item.dependencies)
    if key.startswith("decision:"):
        refs += [f"finding:{ref}" for ref in item.related_findings]
    if key.startswith("plan:"):
        refs += [f"plan:{ref}" for ref in item.depends_on]
    return list(dict.fromkeys(refs))


def _superseded(nodes: dict[str, Any]) -> set[str]:
    result = set()
    for key, item in nodes.items():
        if item.supersedes and item.supersedes != item.id:
            result.add(f"{key.split(':', 1)[0]}:{item.supersedes}")
        if getattr(item, "status", None) == ItemStatus.SUPERSEDED:
            result.add(key)
    return result


def _stale(item: Any) -> bool:
    return bool(item.revalidation_required or getattr(item, "needs_parent_verification", False)
                or isinstance(item, Hypothesis) and item.lifecycle == "stale")


def _concluded(key: str, item: Any) -> bool:
    if isinstance(item, Hypothesis):
        return item.lifecycle in {"confirmed", "excluded"}
    return key.startswith("completed:") or getattr(item, "status", None) == ItemStatus.COMPLETED


def _fresh_host_refs(state: TaskState, refs: list[str]) -> set[str]:
    return {ref for ref in refs if ref in state.evidence_index
            and not state.evidence_index[ref].stale
            and state.evidence_index[ref].provenance == "host_tool_result"
            and state.evidence_index[ref].tool_name not in {
                "", "update_task_state", "compact", "read_artifact", "retrieve_tool_result",
                "memory_write", "memory_read", "task_create", "task_update", "task_get", "task_list",
            }
            and (not state.evidence_index[ref].task_id
                 or state.evidence_index[ref].task_id == state.task_id)}


def _mark_item_stale(item: Any, reason: str) -> bool:
    changed = not item.revalidation_required
    item.revalidation_required = True
    if not item.invalidation_reason:
        item.invalidation_reason = reason
        changed = True
    if isinstance(item, Hypothesis):
        changed |= item.lifecycle != "stale"
        item.lifecycle = "stale"
    if hasattr(item, "status") and item.status != ItemStatus.SUPERSEDED:
        changed |= item.status != ItemStatus.AWAITING_VERIFICATION
        item.status = ItemStatus.AWAITING_VERIFICATION
    if hasattr(item, "needs_parent_verification"):
        item.needs_parent_verification = True
    return changed


def propagate_reasoning_invalidations(state: TaskState, *, changed_nodes: set[str] | None = None) -> bool:
    """Mutate host validity, transitively; bounded fixed point also tolerates old cycles."""
    nodes = reasoning_nodes(state)
    superseded = _superseded(nodes)
    invalid = {key for key, item in nodes.items() if _stale(item)} | superseded
    invalid |= set(changed_nodes or ())
    changed = False
    for key, item in nodes.items():
        if key in superseded:
            continue
        refs = evidence_refs_for(item)
        bad = [ref for ref in refs if ref not in state.evidence_index or state.evidence_index[ref].stale
               or state.evidence_index[ref].task_id and state.evidence_index[ref].task_id != state.task_id]
        if item.task_id and item.task_id != state.task_id:
            bad.append("foreign-task")
        if bad:
            changed |= _mark_item_stale(item, f"evidence requires revalidation: {', '.join(bad[:4])}")
            invalid.add(key)
    # An open hypothesis cannot act as a validated prerequisite for a conclusion.
    invalid |= {key for key, item in nodes.items() if isinstance(item, Hypothesis)
                and item.lifecycle in {"open", "stale"}}
    for _ in range(len(nodes) + 1):
        newly_invalid = set()
        for key, item in nodes.items():
            if key in superseded or key in invalid and _stale(item):
                continue
            bad = [dep for dep in dependencies_for(key, item) if dep in invalid or dep not in nodes]
            if bad:
                changed |= _mark_item_stale(item, f"reasoning prerequisite changed: {', '.join(bad[:4])}")
                newly_invalid.add(key)
        if not newly_invalid - invalid:
            break
        invalid |= newly_invalid
    stale_basis = [ref for ref in state.completion_basis if ref in state.evidence_index
                   and state.evidence_index[ref].stale]
    if stale_basis:
        previous_basis = list(state.completion_basis)
        state.completion_basis = [ref for ref in previous_basis if ref not in stale_basis]
        state.history.append(StateHistoryEntry(
            id=f"history:invalidated-basis:{state.version}:{len(state.history)}",
            category="completion_basis", item_id="completion_basis",
            previous={"refs": previous_basis}, replacement={"refs": list(state.completion_basis)},
            source_patch_id="host:reasoning-invalidation",
        ))
        changed = True
    return changed


def invalidate_reasoning_for_revision(state: TaskState, reason: str = "user requirements changed") -> bool:
    """Host-only revision hook; preserve observations, recheck all interpretations."""
    changed = False
    for item in reasoning_nodes(state).values():
        changed |= _mark_item_stale(item, reason)
    return changed


def validate_reasoning_patch(state: TaskState, patch: Any, error_type: type[Exception]) -> None:
    prospective = deepcopy(state)
    if patch.pending_replace is not None:
        prospective.pending_actions = deepcopy(patch.pending_replace)
    touched: dict[str, Any] = {}
    previous_nodes = reasoning_nodes(state)
    for kind, (attribute, patch_attribute) in _GROUPS.items():
        additions = getattr(patch, patch_attribute)
        current = {item.id: item for item in getattr(prospective, attribute)}
        for item in additions:
            current[item.id] = deepcopy(item)
            touched[f"{kind}:{item.id}"] = item
        setattr(prospective, attribute, list(current.values()))
    if patch.pending_replace is not None:
        touched.update({f"action:{item.id}": item for item in patch.pending_replace})
    for kind, transitions in (("hypothesis", patch.hypothesis_transitions),
                              ("action", patch.action_transitions), ("plan", patch.plan_transitions)):
        for transition in transitions:
            node = reasoning_nodes(prospective).get(f"{kind}:{transition.id}")
            if node is not None:
                node.status = transition.status
                touched[f"{kind}:{transition.id}"] = node
    for evidence in patch.evidence:
        if evidence.task_id and evidence.task_id != state.task_id:
            raise error_type("EvidenceRef belongs to another task")
        if evidence.id not in prospective.evidence_index:
            prospective.evidence_index[evidence.id] = evidence
    nodes = reasoning_nodes(prospective)
    superseded = _superseded(nodes)
    for key, item in touched.items():
        if item.supersedes and item.supersedes != item.id:
            prior_key = f"{key.split(':', 1)[0]}:{item.supersedes}"
            if prior_key not in previous_nodes:
                raise error_type("reasoning supersedes must reference an existing item, not a same-patch chain")
        if item.task_id and item.task_id != state.task_id:
            raise error_type("reasoning item belongs to another task")
        for name in ("dependencies", "evidence_refs", "supporting_evidence_refs", "refuting_evidence_refs"):
            refs = getattr(item, name, [])
            if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
                raise error_type(f"{name} must be an array of nonempty local references")
        for ref in evidence_refs_for(item):
            evidence = prospective.evidence_index.get(ref)
            if evidence is None:
                raise error_type(f"reasoning item references unknown evidence: {ref}")
            if evidence.task_id and evidence.task_id != state.task_id:
                raise error_type("reasoning evidence belongs to another task")
        for dep in dependencies_for(key, item):
            if dep not in nodes:
                raise error_type(f"unknown reasoning dependency: {dep}")
            if nodes[dep].task_id and nodes[dep].task_id != state.task_id:
                raise error_type("reasoning dependency belongs to another task")
        if isinstance(item, Hypothesis):
            if item.lifecycle not in LIFECYCLES:
                raise error_type(f"unknown hypothesis lifecycle: {item.lifecycle}")
            support = set(item.supporting_evidence_refs or item.evidence_refs)
            refuting = set(item.refuting_evidence_refs)
            if support & refuting:
                raise error_type("the same evidence cannot both support and refute a hypothesis")
            if item.lifecycle in {"confirmed", "excluded"}:
                relevant = support if item.lifecycle == "confirmed" else refuting
                if not item.falsifiable_prediction.strip():
                    raise error_type("a concluded hypothesis requires a falsifiable prediction")
                if not _fresh_host_refs(prospective, list(relevant)):
                    raise error_type("a concluded hypothesis requires fresh host tool evidence for its outcome")
                if item.lifecycle == "confirmed" and refuting:
                    raise error_type("a hypothesis with unresolved refuting evidence cannot be confirmed")
            # Legacy status=completed means investigation ended; it is not proof.
            # It deliberately leaves lifecycle=open until explicit evidence review.
        previous = previous_nodes.get(key) or previous_nodes.get(f"{key.split(':', 1)[0]}:{item.supersedes}")
        if previous and previous.revalidation_required and not _stale(item):
            fresh = _fresh_host_refs(prospective, evidence_refs_for(item))
            previous_refs = set(evidence_refs_for(previous))
            if not fresh - previous_refs:
                raise error_type("clearing host reasoning invalidation requires new fresh host evidence")
        if isinstance(item, Hypothesis) and isinstance(previous, Hypothesis):
            removed_refutations = set(previous.refuting_evidence_refs) - set(item.refuting_evidence_refs)
            if item.lifecycle == "confirmed" and removed_refutations:
                if not (_fresh_host_refs(prospective, item.supporting_evidence_refs) - set(evidence_refs_for(previous))):
                    raise error_type("discarding refuting evidence requires new host support")
        if _concluded(key, item):
            if _stale(item):
                # A CompletedItem may intentionally remain pending parent review.
                if not key.startswith("completed:"):
                    raise error_type("invalidated reasoning cannot remain concluded")
            for dep in dependencies_for(key, item):
                target = nodes[dep]
                if dep in superseded or _stale(target):
                    raise error_type("conclusion depends on stale or superseded reasoning")
                if isinstance(target, Hypothesis):
                    if target.lifecycle not in {"confirmed", "excluded"}:
                        raise error_type("conclusion depends on an unresolved hypothesis")
                    if key.startswith("finding:") and target.lifecycle != "confirmed":
                        raise error_type("an excluded hypothesis cannot be promoted to a finding")
    # Check the complete graph without recursion, including legacy edges.
    incoming = {key: 0 for key in nodes}
    dependents: dict[str, list[str]] = {key: [] for key in nodes}
    for key, item in nodes.items():
        for dep in dependencies_for(key, item):
            if dep in nodes:
                incoming[key] += 1
                dependents[dep].append(key)
    ready = [key for key, count in incoming.items() if count == 0]
    visited = 0
    while ready:
        key = ready.pop()
        visited += 1
        for dependent in dependents[key]:
            incoming[dependent] -= 1
            if incoming[dependent] == 0:
                ready.append(dependent)
    if visited != len(nodes):
        raise error_type("reasoning dependency cycle is not allowed")
    checked = deepcopy(prospective)
    # Validate against the actual ordered tool effects as well as proposed
    # interpretation changes; a finalization patch cannot beat invalidation.
    from .evidence_validity import invalidate_after_tool_effect
    for observation in patch.evidence:
        if observation.id not in state.evidence_index:
            checked.evidence_index.pop(observation.id, None)
    for observation in patch.evidence:
        if observation.id not in state.evidence_index:
            invalidate_after_tool_effect(checked, observation, propagate=False)
            checked.evidence_index[observation.id] = deepcopy(observation)
    propagate_reasoning_invalidations(checked, changed_nodes=changed_reasoning_nodes(state, prospective))
    checked_nodes = reasoning_nodes(checked)
    if str(patch.requested_status or "") == "completed":
        excluded = _superseded(checked_nodes)
        if any(key.startswith("completed:") and key not in excluded and _stale(item)
               for key, item in checked_nodes.items()):
            raise error_type("completed task has a reasoning dependency requiring revalidation")
    for key, item in touched.items():
        if _concluded(key, item) and not _stale(item) and _stale(checked_nodes[key]):
            raise error_type("conclusion has a stale evidence dependency chain")


def reasoning_context_view(state: TaskState, max_items: int = 12) -> dict[str, Any]:
    """Bounded planner projection; complete graph stays recoverable at state://current."""
    limit = max(0, min(int(max_items), 50))
    checked = deepcopy(state)
    propagate_reasoning_invalidations(checked)
    nodes = reasoning_nodes(checked)
    superseded = _superseded(nodes)
    active = {key: item for key, item in nodes.items() if key not in superseded}
    hypotheses = [(key, item) for key, item in active.items() if isinstance(item, Hypothesis)]
    hypotheses.sort(key=lambda pair: (pair[1].lifecycle not in {"open", "stale"}, pair[0]))
    chosen = hypotheses[:limit]
    relevant = set(ref for _, item in chosen for ref in evidence_refs_for(item))
    invalidated = [key for key, item in active.items() if _stale(item)]
    evidence = sorted(checked.evidence_index.values(), key=lambda ref: (ref.id not in relevant, ref.id))
    return {
        "task_id": state.task_id,
        "state_version": state.version,
        "current_focus": state.current_focus,
        "interpretation_origin": "model_reasoning_not_tool_verification",
        "hypotheses": [{"ref": key, "claim": item.claim[:600], "lifecycle": item.lifecycle,
                        "supporting_evidence_refs": (item.supporting_evidence_refs or item.evidence_refs)[:limit],
                        "refuting_evidence_refs": item.refuting_evidence_refs[:limit],
                        "falsifiable_prediction": item.falsifiable_prediction[:600],
                        "next_validation": item.next_validation[:600],
                        "dependencies": dependencies_for(key, item)[:limit]} for key, item in chosen],
        "reasoning_items": [{"ref": key,
                             "text": str(getattr(item, "claim", getattr(item, "choice", getattr(item, "description", ""))))[:600],
                             "status": str(getattr(item, "status", "completed")),
                             "revalidation_required": item.revalidation_required,
                             "dependencies": dependencies_for(key, item)[:limit],
                             "evidence_refs": evidence_refs_for(item)[:limit]}
                            for key, item in list(active.items()) if not isinstance(item, Hypothesis)][:limit],
        "invalidated_dependencies": invalidated[:limit],
        "active_evidence": [{"id": ref.id, "uri": ref.uri or f"event://{ref.event_id}",
                             "path": ref.path, "provenance": ref.provenance}
                            for ref in evidence if not ref.stale and (not ref.task_id or ref.task_id == state.task_id)][:limit],
        "omitted_hypotheses": max(0, len(hypotheses) - limit),
        "omitted_invalidated": max(0, len(invalidated) - limit),
        "full_state_ref": "state://current",
    }
