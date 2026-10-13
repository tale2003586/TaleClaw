"""Lossless Responses windows, owned by a session rather than a pooled client.

Opaque reasoning/compaction items are JSON protocol state, never summaries.
Preparing a call is pure; only a completed, validated result can be committed.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
from threading import RLock
from types import SimpleNamespace
from typing import Any, Mapping


NATIVE_CONTEXT_KEY = "native_response_context"
_COMMIT_LOCK = RLock()


class NativeContextConflict(ValueError):
    pass


def native_json(value: Any) -> Any:
    """Serialize SDK items without flattening unknown fields or opaque content."""
    if isinstance(value, Mapping):
        return {str(key): native_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [native_json(item) for item in value]
    if hasattr(value, "model_dump"):
        return native_json(value.model_dump(mode="json", exclude_unset=True))
    if isinstance(value, SimpleNamespace):
        return native_json(vars(value))
    if value is None or isinstance(value, (str, int, float, bool)):
        json.dumps(value, allow_nan=False)
        return value
    raise ValueError(f"Native context contains a non-JSON value: {type(value).__name__}")


def native_items(value: Any) -> list[dict[str, Any]]:
    items = native_json(value)
    if not isinstance(items, list):
        raise ValueError("Native context output must be an item array.")
    for item in items:
        if not isinstance(item, dict) or not (item.get("type") or item.get("role")):
            raise ValueError("Native context item requires type or role.")
        if item.get("type") == "compaction" and (not isinstance(item.get("encrypted_content"), str) or not item["encrypted_content"]):
            raise ValueError("Compaction item requires opaque encrypted_content.")
    return items


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(native_json(value), ensure_ascii=False,
                                    sort_keys=True, allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class NativeContextLineage:
    session_id: str
    model: str
    provider_key: str
    workspace_key: str


@dataclass(frozen=True)
class NativeContextState:
    lineage: NativeContextLineage
    input_items: list[dict[str, Any]]
    event_cursor: int = 0  # Raw session.messages cursor, not a rendered-prompt index.
    revision: int = 0
    expected_assistant_digest: str = ""
    response_id: str = ""
    source_digest: str = ""


@dataclass(frozen=True)
class NativeContextTransaction:
    lineage: NativeContextLineage
    input_items: list[dict[str, Any]]
    event_cursor: int
    base_digest: str
    revision: int
    source_digest: str = ""


def load_native_context(session, lineage: NativeContextLineage) -> NativeContextState | None:
    if str(getattr(session, "id", "")) != lineage.session_id:
        raise NativeContextConflict("Native context belongs to a different session.")
    payload = (getattr(session, "metadata", {}) or {}).get(NATIVE_CONTEXT_KEY)
    if not isinstance(payload, dict) or payload.get("lineage") != asdict(lineage):
        return None
    try:
        return NativeContextState(
            lineage=lineage, input_items=native_items(payload["input_items"]),
            event_cursor=int(payload.get("event_cursor") or 0), revision=int(payload.get("revision") or 0),
            expected_assistant_digest=str(payload.get("expected_assistant_digest") or ""),
            response_id=str(payload.get("response_id") or ""),
            source_digest=str(payload.get("source_digest") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def prepare_native_context(session, lineage: NativeContextLineage, *, new_items,
                           event_cursor: int, source_digest: str = "") -> NativeContextTransaction:
    state = load_native_context(session, lineage)
    payload = (getattr(session, "metadata", {}) or {}).get(NATIVE_CONTEXT_KEY)
    return NativeContextTransaction(
        lineage=lineage, input_items=[*(state.input_items if state else []), *native_items(new_items)],
        event_cursor=max(0, int(event_cursor)), base_digest=_digest(payload),
        revision=(state.revision if state else 0), source_digest=source_digest,
    )


def commit_native_context(session, transaction: NativeContextTransaction, response, *,
                          expected_assistant_digest: str = "") -> NativeContextState:
    from runtime.execution.budget import current_budget
    if current_budget() is not None:
        current_budget().check()
    if str(getattr(session, "id", "")) != transaction.lineage.session_id:
        raise NativeContextConflict("Native context belongs to a different session.")
    with _COMMIT_LOCK:
        current = (getattr(session, "metadata", {}) or {}).get(NATIVE_CONTEXT_KEY)
        if _digest(current) != transaction.base_digest:
            raise NativeContextConflict("Native context changed while the provider call was running.")
        metadata = getattr(response, "provider_metadata", {}) or {}
        native = metadata.get("native_context")
        if not isinstance(native, dict) or native.get("model") != transaction.lineage.model:
            raise NativeContextConflict("Completed native response model does not match its lineage.")
        output = native_items(native.get("output_items"))
        mode = native.get("mode")
        if mode not in {"append", "replace"}:
            raise NativeContextConflict("Native response has no completed window operation.")
        items = output if mode == "replace" else [*transaction.input_items, *output]
        payload = {
            "version": 1, "lineage": asdict(transaction.lineage), "input_items": items,
            "event_cursor": transaction.event_cursor, "revision": transaction.revision + 1,
            "expected_assistant_digest": expected_assistant_digest,
            "response_id": str(native.get("response_id") or ""),
            "source_digest": transaction.source_digest,
        }
        session.metadata[NATIVE_CONTEXT_KEY] = deepcopy(payload)
        state = load_native_context(session, transaction.lineage)
        assert state is not None
        return state


def assistant_message_digest(message: dict[str, Any]) -> str:
    return _digest({"role": message.get("role"), "content": message.get("content") or None,
                    "tool_calls": message.get("tool_calls") or []})
