from __future__ import annotations

import os
from typing import Any

from runtime.trace.trace_store import event_preview


def security_rag_runtime_trace_enabled() -> bool:
    value = os.getenv("SECURITY_RAG_RUNTIME_TRACE_ENABLED", "1")
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def append_security_rag_event(
    trace_store,
    run_state,
    event_name: str,
    payload: dict[str, Any] | None = None,
    *,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    step: int | None = None,
) -> None:
    if trace_store is None or run_state is None:
        return
    if not security_rag_runtime_trace_enabled():
        return
    trace_store.append_event(
        run_state,
        event_name,
        _json_safe(payload or {}),
        span_id=span_id,
        parent_span_id=parent_span_id,
        step=step,
    )


def security_rag_span_id(run_state, parent_span_id: str | None, source: str) -> str | None:
    prefix = parent_span_id or getattr(run_state, "run_id", None)
    if not prefix:
        return None
    normalized_source = str(source or "rag").replace(" ", "_").replace(":", "_")
    return f"{prefix}:security_rag:{normalized_source}"


def decision_to_trace_payload(decision: Any) -> dict[str, Any] | None:
    if decision is None:
        return None
    if hasattr(decision, "to_dict"):
        return _json_safe(decision.to_dict())
    if isinstance(decision, dict):
        return _json_safe(decision)
    return {
        "use_rag": bool(getattr(decision, "use_rag", False)),
        "route": str(getattr(decision, "route", "") or ""),
        "query": str(getattr(decision, "query", "") or ""),
        "confidence": _float(getattr(decision, "confidence", 0.0)),
        "reason": str(getattr(decision, "reason", "") or ""),
    }


def hit_to_trace_payload(hit: Any, *, include_text_preview: bool = False) -> dict[str, Any]:
    payload = {
        "id": str(getattr(hit, "id", "") or ""),
        "score": _float(getattr(hit, "score", 0.0)),
        "source": str(getattr(hit, "source_relpath", "") or ""),
        "title": str(getattr(hit, "title", "") or ""),
        "chunk_index": _int(getattr(hit, "chunk_index", 0)),
    }
    metadata = getattr(hit, "metadata", None)
    if isinstance(metadata, dict):
        payload["metadata"] = _json_safe(metadata)
    if include_text_preview:
        payload["text_preview"] = event_preview(getattr(hit, "text", "") or "", limit=240)
    return payload


def hits_to_trace_payload(
    hits: list[Any] | tuple[Any, ...],
    *,
    limit: int = 5,
    include_text_preview: bool = False,
) -> list[dict[str, Any]]:
    return [
        hit_to_trace_payload(hit, include_text_preview=include_text_preview)
        for hit in list(hits or [])[: max(0, int(limit))]
    ]


def search_trace_payload(
    *,
    source: str,
    query: str,
    stage: str = "",
    retrieval_mode: str = "",
    use_reranker: bool | None = None,
    top_k: int | None = None,
    min_score: float | None = None,
    use_cache: bool | None = None,
    hit_count: int | None = None,
    tier: str = "",
    top_score: float | None = None,
    score_gap: float | None = None,
    source_concentration: float | None = None,
    trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trace = trace or {}
    latency = trace.get("latency_ms") if isinstance(trace.get("latency_ms"), dict) else {}
    payload = {
        "source": source,
        "query": query,
        "stage": stage,
        "retrieval_mode": retrieval_mode or str(trace.get("retrieval_mode") or ""),
        "hybrid_enabled": bool(trace.get("hybrid_enabled", False)),
        "reranker_enabled": (
            bool(use_reranker)
            if use_reranker is not None
            else bool(trace.get("reranker_enabled", False))
        ),
        "top_k": top_k,
        "min_score": min_score,
        "use_cache": use_cache,
        "cache_hit": bool(trace.get("cache_hit", False)),
        "candidate_count": _optional_int(trace.get("candidate_count")),
        "final_count": _optional_int(trace.get("final_count")),
        "hit_count": hit_count,
        "tier": tier,
        "top_score": top_score,
        "score_gap": score_gap,
        "source_concentration": source_concentration,
        "latency_ms": _json_safe(latency),
    }
    return {key: value for key, value in payload.items() if value is not None}


def rag_completed_payload(
    *,
    source: str,
    query: str,
    rewritten_query: str,
    decision: Any = None,
    action: str = "",
    reason: str = "",
    hits: list[Any] | None = None,
    latency_ms: dict[str, Any] | None = None,
    searches: list[Any] | None = None,
) -> dict[str, Any]:
    hits = list(hits or [])
    return {
        "source": source,
        "query": query,
        "rewritten_query": rewritten_query or query,
        "query_rewritten": bool((rewritten_query or query) != query),
        "decision": decision_to_trace_payload(decision),
        "route": str(getattr(decision, "route", "") or ""),
        "use_rag": bool(getattr(decision, "use_rag", bool(hits))),
        "action": action,
        "reason": reason,
        "hit_count": len(hits),
        "top_score": _float(getattr(hits[0], "score", 0.0)) if hits else 0.0,
        "hits": hits_to_trace_payload(hits),
        "latency_ms": _json_safe(latency_ms or {}),
        "searches": [_search_record_to_payload(record) for record in searches or []],
    }


def _search_record_to_payload(record: Any) -> dict[str, Any]:
    if hasattr(record, "__dict__"):
        return _json_safe(dict(record.__dict__))
    if isinstance(record, dict):
        return _json_safe(record)
    return {}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
