from __future__ import annotations

import json

from knowledge.security_rag import build_security_index_from_env
from knowledge.tracing import Timer, make_rag_trace, write_rag_trace_if_enabled
from plugins.base import Plugin, ToolRegistration
from runtime.trace.events import (
    SECURITY_RAG_COMPLETED,
    SECURITY_RAG_FAILED,
    SECURITY_RAG_SEARCH_COMPLETED,
    SECURITY_RAG_STARTED,
)
from runtime.trace.rag import (
    append_security_rag_event,
    rag_completed_payload,
    search_trace_payload,
    security_rag_span_id,
)
from tools.schema import function_tool


class SecurityRagPlugin(Plugin):
    name = "security_rag"

    def tools(self) -> list[ToolRegistration]:
        return [
            ToolRegistration(
                schema=function_tool(
                    "security_rag_search",
                    "Search the local code security RAG knowledge base for secure coding, vulnerability, CWE/CVE/GHSA, auth, authorization, injection, XSS, SSRF, token, secrets, dependency, file upload, and path traversal guidance. Use this before answering security-related questions that need local evidence.",
                    {
                        "query": {
                            "type": "string",
                            "description": "Security-focused search query. English or mixed Chinese/English usually works best.",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Number of chunks to return. Defaults to 5, maximum 10.",
                        },
                        "min_score": {
                            "type": "number",
                            "description": "Optional minimum vector similarity score.",
                        },
                    },
                    ["query"],
                ),
                handler=self.search,
                risk="low",
                allowed_agents={"bot", "coding"},
                always_on=True,
                source="plugin:security_rag",
            )
        ]

    def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.0,
        *,
        _trace_store=None,
        _run_state=None,
        _parent_span_id: str | None = None,
    ) -> str:
        index = build_security_index_from_env()
        timer = Timer()
        search_trace = {}
        normalized_top_k = max(1, min(10, int(top_k or 5)))
        normalized_min_score = float(min_score or 0.0)
        rag_span_id = security_rag_span_id(_run_state, _parent_span_id, "security_rag_search")

        def append_runtime_event(event_name: str, payload: dict) -> None:
            append_security_rag_event(
                _trace_store,
                _run_state,
                event_name,
                {"source": "security_rag_search_tool", **payload},
                span_id=(
                    rag_span_id
                    if event_name in {SECURITY_RAG_STARTED, SECURITY_RAG_COMPLETED, SECURITY_RAG_FAILED}
                    else None
                ),
                parent_span_id=(
                    _parent_span_id
                    if event_name in {SECURITY_RAG_STARTED, SECURITY_RAG_COMPLETED, SECURITY_RAG_FAILED}
                    else rag_span_id
                ),
            )

        append_runtime_event(SECURITY_RAG_STARTED, {
            "query": query,
            "entrypoint": "tool",
            "top_k": normalized_top_k,
            "min_score": normalized_min_score,
        })
        try:
            hits = index.search(
                query=query,
                top_k=normalized_top_k,
                min_score=normalized_min_score,
                trace_callback=search_trace.update,
            )
        except Exception as exc:
            elapsed_ms = timer.ms()
            append_runtime_event(SECURITY_RAG_FAILED, {
                "query": query,
                "stage": "search",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "latency_ms": {"total": elapsed_ms},
            })
            write_rag_trace_if_enabled(make_rag_trace(
                source="security_rag_search_tool",
                query=query,
                latency_ms={"total": elapsed_ms},
                error=f"search_error:{type(exc).__name__}: {exc}",
            ))
            raise
        append_runtime_event(
            SECURITY_RAG_SEARCH_COMPLETED,
            search_trace_payload(
                source="security_rag_search_tool",
                query=query,
                stage="tool_search",
                retrieval_mode=str(search_trace.get("retrieval_mode") or ""),
                top_k=normalized_top_k,
                min_score=normalized_min_score,
                hit_count=len(hits),
                trace=search_trace,
            ),
        )
        elapsed_ms = timer.ms()
        append_runtime_event(SECURITY_RAG_COMPLETED, rag_completed_payload(
            source="security_rag_search_tool",
            query=query,
            rewritten_query=query,
            action="tool_search",
            reason="security_rag_search tool completed.",
            hits=hits,
            latency_ms={
                "search": elapsed_ms,
                **(search_trace.get("latency_ms") or {}),
                "total": elapsed_ms,
            },
        ))
        write_rag_trace_if_enabled(make_rag_trace(
            source="security_rag_search_tool",
            query=query,
            hits=hits,
            latency_ms={
                "search": elapsed_ms,
                **(search_trace.get("latency_ms") or {}),
                "total": elapsed_ms,
            },
        ))
        return json.dumps(
            [
                {
                    "score": hit.score,
                    "source": hit.source_relpath,
                    "title": hit.title,
                    "chunk_index": hit.chunk_index,
                    "text": hit.text,
                    "metadata": hit.metadata,
                }
                for hit in hits
            ],
            ensure_ascii=False,
            indent=2,
        )
