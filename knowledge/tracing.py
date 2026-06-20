from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import uuid
from typing import Any


@dataclass(frozen=True)
class RagTrace:
    trace_id: str
    timestamp: str
    source: str
    query: str
    rewritten_query: str
    router_decision: dict | None
    final_hits: list[dict]
    latency_ms: dict[str, float]
    error: str | None = None
    user_feedback: str | None = None


class RagTraceStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or os.getenv("SECURITY_RAG_TRACE_DIR", "~/.claude/rag_traces")).expanduser()

    def write(self, trace: RagTrace) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        date = trace.timestamp[:10]
        path = self.root / f"rag_traces_{date}.jsonl"
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(asdict(trace), ensure_ascii=False) + "\n")
        return path


def trace_enabled() -> bool:
    return str(os.getenv("SECURITY_RAG_TRACE_ENABLED", "1")).strip().lower() not in {"0", "false", "no", "off"}


def make_rag_trace(
    *,
    source: str,
    query: str,
    rewritten_query: str = "",
    router_decision: Any = None,
    hits: list[Any] | None = None,
    latency_ms: dict[str, float] | None = None,
    error: str | None = None,
) -> RagTrace:
    return RagTrace(
        trace_id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc).isoformat(),
        source=source,
        query=query,
        rewritten_query=rewritten_query or query,
        router_decision=router_decision.to_dict() if hasattr(router_decision, "to_dict") else router_decision,
        final_hits=[hit_to_trace_dict(hit) for hit in hits or []],
        latency_ms=latency_ms or {},
        error=error,
    )


def hit_to_trace_dict(hit: Any) -> dict:
    return {
        "id": getattr(hit, "id", ""),
        "score": float(getattr(hit, "score", 0.0) or 0.0),
        "source": getattr(hit, "source_relpath", ""),
        "title": getattr(hit, "title", ""),
        "chunk_index": getattr(hit, "chunk_index", 0),
        "metadata": getattr(hit, "metadata", {}) if isinstance(getattr(hit, "metadata", None), dict) else {},
    }


class Timer:
    def __init__(self) -> None:
        self.started = time.perf_counter()

    def ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000


def write_rag_trace_if_enabled(trace: RagTrace) -> None:
    if not trace_enabled():
        return
    try:
        RagTraceStore().write(trace)
    except Exception:
        return
