"""Content-free evidence that a policy changed a submitted context window."""
from __future__ import annotations

import json
from typing import Any


def request_context_intervention(session, messages: list[dict], policy: str) -> dict[str, Any]:
    metadata = getattr(session, "metadata", {}) or {}
    plan = metadata.get("context_policy_audit") or {}
    complete = isinstance(plan, dict) and plan.get("version") == 1 and plan.get("policy") == policy
    result = {"schema_version": 1, "policy": policy, "audit_complete": complete,
              "scope": "final_local_request", "masked_observations": 0, "dropped_groups": 0,
              "snapshot_id": str(getattr(session, "active_snapshot_id", "") or "") if policy == "current" else "",
              "planner_token_reduction_estimate": None}
    # Count actual final messages, not abstract plan labels: small observations
    # can be labeled abstract while their source text is kept unchanged.
    for message in messages:
        if message.get("role") != "tool" or (message.get("metadata") or {}).get("context_projection") != "masked":
            continue
        try:
            envelope = json.loads(message.get("content") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(envelope, dict) and envelope.get("observation_masked") is True and envelope.get("source_ref"):
            result["masked_observations"] += 1
    if policy in {"adaptive", "masking", "raw", "native"}:
        decisions = plan.get("decisions") if isinstance(plan, dict) else None
        if not isinstance(decisions, list):
            result["audit_complete"] = False
        else:
            source = list(getattr(session, "messages", []) or [])
            visible = {_message_key(message) for message in messages}
            for decision in decisions:
                if not isinstance(decision, dict):
                    result["audit_complete"] = False
                    continue
                if decision.get("mode") != "drop":
                    continue
                start, end = decision.get("start"), decision.get("end")
                if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source):
                    result["audit_complete"] = False
                    continue
                # Reappearing duplicate content is conservative: it cannot prove
                # removal of that source from the final prompt.
                if all(_message_key(message) not in visible for message in source[start:end]):
                    result["dropped_groups"] += 1
            before, after = plan.get("before_tokens"), plan.get("after_tokens")
            if type(before) is int and type(after) is int:
                result["planner_token_reduction_estimate"] = max(0, before - after)
    if policy == "native":
        # Native state is audited from completed response metadata, not from
        # this local placeholder assembled before provider-native execution.
        result.update(scope="native_pending_response", masked_observations=0, dropped_groups=0, snapshot_id="")
    return result


def _message_key(message: dict) -> str:
    return json.dumps({key: message.get(key) for key in ("role", "content", "tool_call_id", "tool_calls")},
                      sort_keys=True, ensure_ascii=False, default=str)
