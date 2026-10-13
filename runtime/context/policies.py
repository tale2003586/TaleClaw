"""Replaceable, deterministic context projections over an immutable source log.

Policies decide visibility, never evidence truth or whether a task passed. A plan
can be rebuilt with a different focus without deleting or summarizing its source.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, fields, replace, field
import hashlib
from html import escape
import json
import re
from typing import Any

from runtime.context.events import is_real_user_message, thaw
from runtime.token_estimator import estimate_tokens

POLICY_NAMES = ("current", "raw", "masking", "adaptive", "native")


@dataclass(frozen=True)
class ContextPolicy:
    name: str = "current"
    recent_groups: int = 2
    preview_chars: int = 320
    soft_ratio: float = 0.70
    target_ratio: float = 0.45
    native_mode: str = "server"
    native_compact_threshold: int | None = None
    native_fallback: str = "error"

    def __post_init__(self):
        if self.name not in POLICY_NAMES:
            raise ValueError(f"Unknown context policy: {self.name}")
        if type(self.recent_groups) is not int or not 1 <= self.recent_groups <= 20:
            raise ValueError("recent_groups must be an integer between 1 and 20")
        if type(self.preview_chars) is not int or not 0 <= self.preview_chars <= 4000:
            raise ValueError("preview_chars must be an integer between 0 and 4000")
        if not 0 < self.target_ratio < self.soft_ratio < 1:
            raise ValueError("Require 0 < target_ratio < soft_ratio < 1")
        if self.native_mode not in {"server", "standalone"}:
            raise ValueError("native_mode must be server or standalone")
        if self.native_fallback not in {"error", "current", "masking", "adaptive", "raw"}:
            raise ValueError("native_fallback must be error or a local policy")
        if self.native_compact_threshold is not None and (
            type(self.native_compact_threshold) is not int or self.native_compact_threshold < 1
        ):
            raise ValueError("native_compact_threshold must be a positive integer")


def context_policy(session) -> ContextPolicy:
    metadata = getattr(session, "metadata", {}) or {}
    value = metadata.get("context_policy", "current")
    if metadata.get("context_policy_resolved_from") == value:
        value = metadata.get("context_policy_effective", value)
    if isinstance(value, str):
        return ContextPolicy(name=value)
    if not isinstance(value, dict):
        raise ValueError("context_policy must be a policy name or object")
    unknown = set(value) - {item.name for item in fields(ContextPolicy)}
    if unknown:
        raise ValueError(f"Unknown context policy settings: {sorted(unknown)}")
    return ContextPolicy(**value)


def request_compaction(session, *, focus: str = "") -> dict[str, Any]:
    """Queue only; the next builder runs after ALL pending tool results close."""
    metadata = session.metadata
    count = len(session.messages)
    previous = metadata.get("context_compaction_request") or {}
    if previous and (previous.get("status") == "pending" or count - int(previous.get("message_count", 0)) < 4):
        return {"accepted": False, "reason": "pending_or_no_new_work"}
    request = {"status": "pending", "message_count": count, "focus": str(focus)[:2000]}
    metadata["context_compaction_request"] = request
    return {"accepted": True, "reason": "next_safe_context_boundary"}


@dataclass(frozen=True)
class ContextPlan:
    messages: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    before_tokens: int
    after_tokens: int
    target_tokens: int
    policy: str
    source_digest: str
    execution_index: dict[str, Any] = field(default_factory=dict)

    def audit(self) -> dict[str, Any]:
        counts = {mode: sum(row["mode"] == mode for row in self.decisions) for mode in ("raw", "abstract", "drop")}
        return {"version": 1, "policy": self.policy, "source_digest": self.source_digest,
                "before_tokens": self.before_tokens, "after_tokens": self.after_tokens,
                "target_tokens": self.target_tokens, "over_target": self.after_tokens > self.target_tokens,
                "group_counts": counts, "decisions": self.decisions,
                "execution_index": self.execution_index}


def _terms(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_terms(v) for v in value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(_terms(v) for v in value)) if value else set()
    return set(re.findall(r"[\w./:-]{3,}", str(value).lower()))


def _event_refs(session) -> dict[int, str]:
    events = session.events_after(0) if hasattr(session, "events_after") else getattr(session, "event_log", [])
    result = {}
    positions = {}
    for index, message in enumerate(session.messages):
        key = json.dumps(message, sort_keys=True, ensure_ascii=False, default=str)
        positions.setdefault(key, []).append(index)
    for event in events:
        payload = thaw(event.payload)
        message = payload.get("message")
        if not isinstance(message, dict):
            continue
        index = payload.get("legacy_message_index")
        if type(index) is not int:
            key = json.dumps(message, sort_keys=True, ensure_ascii=False, default=str)
            index = next((i for i in positions.get(key, []) if i not in result), None)
        if type(index) is int:
            result[index] = f"event://{event.event_id}"
    return result


def _abstract(group, refs: dict[int, str], preview_chars: int) -> list[dict[str, Any]]:
    result = deepcopy(group.messages)
    for offset, message in enumerate(result):
        if message.get("role") != "tool":
            continue
        ref = refs.get(group.start + offset)
        # No recovery address: keep the full observation. Never invent a ref.
        if not ref:
            continue
        raw = str(message.get("content") or "")
        envelope = {"observation_masked": True, "instructions": False,
                    "source_ref": ref, "source_chars": len(raw),
                    "source_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                    "preview": raw[:preview_chars],
                    "recovery": "read_artifact(artifact_ref=source_ref); preview is incomplete, not new evidence"}
        content = json.dumps(envelope, ensure_ascii=False)
        if len(content) < len(raw):
            message["content"] = content
            metadata = message.get("metadata")
            message["metadata"] = {**(metadata if isinstance(metadata, dict) else {}),
                                   "context_projection": "masked", "recovery_ref": ref}
    return result


def _adaptive_budget_groups(groups):
    """Protect user source text without pinning an adjacent assistant essay.

    The source grouper pairs a user request with its plain assistant response.
    They need separate visibility decisions under pressure; function calls and
    their tool results still form one indivisible transaction. Source positions
    remain unchanged so every discarded response retains its original event ref.
    """
    result = []
    for group in groups:
        if (group.closed and group.end - group.start == 2 and len(group.messages) == 2
                and is_real_user_message(group.messages[0])
                and group.messages[1].get("role") == "assistant"
                and not group.messages[1].get("tool_calls")):
            result.extend([
                replace(group, end=group.start + 1, messages=group.messages[:1], kind="user"),
                replace(group, start=group.start + 1, messages=group.messages[1:], kind="assistant"),
            ])
        else:
            result.append(group)
    return result


def _execution_index(rows, refs, *, max_tokens: int, dropped_only: bool = True):
    """Bounded host receipts for omitted transactions, never result summaries."""
    if max_tokens <= 0:
        return None
    receipts = {}
    transactions = 0
    for row in rows:
        if row["protected"] or (dropped_only and row["mode"] != "drop"):
            continue
        group = row["group"]
        calls = {str(call.get("id") or ""): call for message in group.messages
                 for call in message.get("tool_calls") or [] if isinstance(call, dict)}
        for offset, message in enumerate(group.messages):
            if message.get("role") != "tool":
                continue
            call = calls.get(str(message.get("tool_call_id") or ""))
            ref = refs.get(group.start + offset)
            if call is None or not ref:
                continue
            function = call.get("function")
            function = function if isinstance(function, dict) else {}
            arguments = message.get("final_arguments", function.get("arguments") or {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {"unparsed_arguments": arguments}
            arguments = arguments if isinstance(arguments, dict) else {}
            name = str(function.get("name") or call.get("name") or "unknown")
            metadata = message.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            status = message.get("status") or metadata.get("status") or "recorded"
            status = status if isinstance(status, str) and status in {"success", "error", "denied", "timeout", "cancelled", "unknown"} else "recorded"
            signature = json.dumps([name, arguments, status, message.get("content")],
                                   sort_keys=True, ensure_ascii=False, default=str)
            key = hashlib.sha256(signature.encode()).hexdigest()
            old = receipts.pop(key, None)
            projected = {}
            for field_name in ("path", "artifact_ref", "query", "command", "pattern", "offset", "limit"):
                value = arguments.get(field_name)
                if field_name in arguments:
                    projected[field_name] = (value if value is None or type(value) in {int, bool, float}
                                             else str(value)[:160])
            receipt = {"tool": name[:80], "args": projected, "status": status, "source_ref": ref}
            if projected != arguments:
                receipt["args_incomplete"] = True
            receipt["occurrences"] = int(old.get("occurrences", 0)) + 1 if old else 1
            receipts[key] = receipt
            transactions += 1
    if not receipts:
        return None
    chosen = []
    def message():
        omitted = transactions - sum(item["occurrences"] for item in chosen)
        payload = {"scope": "execution_receipts_not_task_verification", "receipts": chosen,
                   "omitted_receipts": omitted,
                   "recovery": "read_artifact(artifact_ref='event://session', query='tool name or path'); omitted does not mean never attempted"}
        return {"role": "system", "content": '<context-execution-index instructions="false">\n' + escape(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")), quote=False,
        ) + '\n</context-execution-index>',
            "metadata": {"origin": "runtime", "kind": "context_execution_index", "instructions": False}}
    if estimate_tokens([message()]) > max_tokens:
        return None
    for item in reversed(list(receipts.values())):
        if len(chosen) >= 12:
            break
        chosen.insert(0, item)
        if estimate_tokens([message()]) > max_tokens:
            chosen.pop(0)
    return message() if chosen else None


def plan_context(session, *, groups, policy: ContextPolicy, max_tokens: int, focus: Any = None) -> ContextPlan:
    """Keep user sources and open transactions; select whole closed groups.

    Masking only replaces old observations. Adaptive starts with recoverable
    observations, drops lower relevance groups, then restores relevant raw data
    if budget permits. Every invocation starts from SOURCE, not its previous plan.
    """
    refs = _event_refs(session)
    # Preserve the configured number of original recent groups even when an
    # older user/assistant turn gets two independent visibility decisions.
    recent_start = groups[max(0, len(groups) - policy.recent_groups)].start if groups else 0
    if policy.name == "adaptive":
        groups = _adaptive_budget_groups(groups)
    focus_terms = _terms(focus or {})
    priority_terms = _terms((focus.get("reasoning") or {}).get("current_focus", "")) if isinstance(focus, dict) else set()
    if isinstance(focus, dict):
        priority_terms |= _terms(focus.get("request", ""))
    source = [message for group in groups for message in group.messages]
    rows = []
    for index, group in enumerate(groups):
        protected = (not group.closed or group.start >= recent_start
                     or any(i not in refs for i in range(group.start, group.end))
                     or any(is_real_user_message(m) or m.get("role") in {"system", "developer"}
                            for m in group.messages))
        raw = deepcopy(group.messages)
        abstract = raw if protected else _abstract(group, refs, policy.preview_chars)
        raw_terms = _terms(raw)
        relevant = len(focus_terms & raw_terms) + 10 * len(priority_terms & raw_terms) + 4 * len(focus_terms & _terms([refs.get(i, "") for i in range(group.start, group.end)]))
        rows.append({"group": group, "raw": raw, "abstract": abstract, "protected": protected,
                     "costs": {"raw": estimate_tokens(raw), "abstract": estimate_tokens(abstract), "drop": 0},
                     "score": relevant, "mode": "raw" if protected or policy.name in {"raw", "native", "current"} else "abstract"})
    def output():
        return [m for row in rows if row["mode"] != "drop" for m in row[row["mode"]]]
    index_audit = {"status": "not_applicable"}
    if policy.name == "adaptive":
        # Reserve the bounded receipt index inside the selection budget. A
        # dropped observation must not erase the fact that its call occurred.
        index_budget = min(768, max(0, max_tokens // 4))
        possible_index = _execution_index(rows, refs, max_tokens=index_budget, dropped_only=False)
        index_reserve = estimate_tokens([possible_index]) if possible_index else 0
        selection_budget = max(0, max_tokens - index_reserve)
        retained_transactions = 0
        for row in rows:
            has_results = any(message.get("role") == "tool" for message in row["group"].messages)
            row["drop_allowed"] = bool(possible_index) or not has_results
            if not row["protected"] and not row["drop_allowed"]:
                retained_transactions += 1
        index_audit = {"status": "unavailable_budget" if retained_transactions else "not_needed",
                       "reserved_tokens": index_reserve, "retained_abstract_transactions": retained_transactions}
        # If even one receipt cannot fit, preserve the abstract transaction and
        # report over_target. The final hard guard decides whether it can run;
        # silently erasing all progress is not a valid budget fallback.
        total = sum(r["costs"][r["mode"]] for r in rows)
        ordered = sorted((r for r in rows if not r["protected"] and r["drop_allowed"]),
                         key=lambda r: (r["score"], r["group"].start))
        for row in ordered:
            if total <= selection_budget:
                break
            total -= row["costs"][row["mode"]]
            row["mode"] = "drop"
        # A high-value raw observation can displace less relevant previews.
        # Plan the entire swap first, so a failed upgrade changes nothing.
        for row in sorted((r for r in rows if not r["protected"] and r["score"] > 0),
                          key=lambda r: (-r["score"], -r["group"].start)):
            proposed = total + row["costs"]["raw"] - row["costs"][row["mode"]]
            victims = []
            for candidate in ordered:
                if proposed <= selection_budget:
                    break
                if candidate is not row and candidate["score"] < row["score"] and candidate["mode"] != "drop":
                    victims.append(candidate)
                    proposed -= candidate["costs"][candidate["mode"]]
            if proposed <= selection_budget:
                for victim in victims:
                    victim["mode"] = "drop"
                row["mode"] = "raw"
                total = proposed
    messages = output()
    if policy.name == "adaptive":
        index = _execution_index(rows, refs, max_tokens=index_reserve)
        if index is not None:
            messages.insert(0, index)
            index_audit = {"status": "included", "reserved_tokens": index_reserve,
                           "actual_tokens": estimate_tokens([index]), "retained_abstract_transactions": 0}
        else:
            restored = 0
            for row in rows:
                if row["mode"] == "drop" and any(m.get("role") == "tool" for m in row["group"].messages):
                    row["mode"] = "abstract"
                    restored += 1
            if restored:
                messages = output()
                index_audit = {"status": "unavailable_budget", "reserved_tokens": index_reserve,
                               "retained_abstract_transactions": restored}
    decisions = [{"start": r["group"].start, "end": r["group"].end, "mode": r["mode"],
                  "reason": "protected_user_or_open_or_recent_or_unrecoverable" if r["protected"] else
                            ("focus_relevance" if r["score"] else "older_observation"),
                  "relevance": r["score"], "source_refs": [refs[i] for i in range(r["group"].start, r["group"].end) if i in refs]}
                 for r in rows]
    digest = hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    return ContextPlan(messages, decisions, estimate_tokens(source), estimate_tokens(messages),
                       max_tokens, policy.name, digest, execution_index=index_audit)


def resolve_context_policy(session, provider, model=None) -> ContextPolicy:
    """Resolve explicit native capability fallback before constructing the view."""
    metadata = session.metadata
    metadata.pop("context_policy_resolved_from", None)
    metadata.pop("context_policy_effective", None)
    requested = context_policy(session)
    effective = requested
    reason = ""
    if requested.name == "native" and provider is not None:
        from models.provider import NativeContextUnsupported
        from runtime.context.native_policy import _resolve_provider
        try:
            selected, _, _ = _resolve_provider(provider, model)
            capabilities = selected.native_context_capabilities()
            key = "server_compaction" if requested.native_mode == "server" else "standalone_compaction"
            if not capabilities.get(key) or not capabilities.get("input_token_count"):
                raise NativeContextUnsupported(f"Native {requested.native_mode} requires {key} and input-token counting")
        except NativeContextUnsupported as exc:
            if requested.native_fallback == "error":
                metadata["context_policy_resolution"] = {"requested": "native", "effective": None,
                    "unsupported": True, "fallback_reason": str(exc)}
                raise
            from dataclasses import replace
            effective = replace(requested, name=requested.native_fallback)
            reason = str(exc)
    metadata["context_policy_resolution"] = {"requested": requested.name, "effective": effective.name, "fallback_reason": reason}
    metadata["context_policy_resolved_from"] = deepcopy(metadata.get("context_policy", "current"))
    metadata["context_policy_effective"] = asdict(effective)
    return effective
