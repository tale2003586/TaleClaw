"""Optional history and security retrieval used by full Context composition."""

from __future__ import annotations

from typing import Any

class ContextRetrievalService:
    def __init__(
        self,
        *,
        history_vector_index=None,
        history_scope_resolver=None,
        retrieval_top_k: int = 6,
        retrieval_min_score: float = 0.35,
        security_retrieval_router=None,
        security_route_classifier=None,
        security_knowledge_index=None,
        security_auto_context_enabled: bool = True,
        episodic_retrieval_service=None,
    ) -> None:
        self.history_vector_index = history_vector_index
        self.history_scope_resolver = history_scope_resolver or (
            lambda session: str(getattr(session, "id", "") or "global")
        )
        self.retrieval_top_k = max(1, int(retrieval_top_k))
        self.retrieval_min_score = float(retrieval_min_score)
        self.security_retrieval_router = security_retrieval_router
        self.security_route_classifier = security_route_classifier
        self.security_knowledge_index = security_knowledge_index
        self.security_auto_context_enabled = bool(security_auto_context_enabled)
        self.episodic_retrieval_service = episodic_retrieval_service

    def retrieve_history(
        self,
        *,
        session,
        current_request: str,
        active_turn_messages: list[dict],
    ) -> tuple[str, list]:
        if self.history_vector_index is None:
            return "", []
        query = self._retrieval_query(current_request, active_turn_messages)
        if not query.strip():
            return "", []
        service, boundary_type = self._episodic_retrieval()
        result = service.retrieve(
            query,
            boundary_type.from_session(session),
        )
        if hasattr(service, "drain_trace_events"):
            events = service.drain_trace_events()
            metadata = getattr(session, "metadata", None)
            if events and isinstance(metadata, dict):
                metadata.setdefault("memory_trace_events", []).extend(events)
        return service.render(result), list(result.hits)

    def _episodic_retrieval(self):
        from memory.episodic_retrieval import (
            EpisodicBoundary,
            EpisodicHistoryRetrievalService,
        )

        if self.episodic_retrieval_service is None:
            self.episodic_retrieval_service = EpisodicHistoryRetrievalService(
                self.history_vector_index,
                top_k=self.retrieval_top_k,
                min_score=self.retrieval_min_score,
            )
        return self.episodic_retrieval_service, EpisodicBoundary

    def _retrieval_query(self, current_request: str, active_turn_messages: list[dict]) -> str:
        if current_request.strip():
            return current_request.strip()
        parts = []
        for message in active_turn_messages[-4:]:
            if not isinstance(message, dict):
                continue
            role = message.get("role", "")
            if role in {"user", "assistant"}:
                parts.append(_message_text(message))
        return "\n".join(part for part in parts if part.strip())

    def retrieve_security(
        self,
        *,
        current_request: str,
        trace_store=None,
        run_state=None,
        trace_parent_span_id: str | None = None,
        reasoning_step: int | None = None,
    ) -> tuple[str, Any | None, list]:
        from knowledge.tracing import Timer, make_rag_trace, write_rag_trace_if_enabled
        from runtime.trace.events import (
            SECURITY_RAG_COMPLETED,
            SECURITY_RAG_FAILED,
            SECURITY_RAG_SEARCH_COMPLETED,
            SECURITY_RAG_SEARCH_FAILED,
            SECURITY_RAG_STARTED,
        )
        from runtime.trace.rag import (
            append_security_rag_event,
            rag_completed_payload,
            search_trace_payload,
            security_rag_span_id,
        )

        if not self.security_auto_context_enabled:
            return "", None, []
        if not current_request.strip():
            return "", None, []
        if self.security_retrieval_router is None or self.security_knowledge_index is None:
            return "", None, []
        total_timer = Timer()
        source = "context_auto"
        rag_span_id = security_rag_span_id(run_state, trace_parent_span_id, source)

        def append_runtime_event(event_name: str, payload: dict[str, Any]) -> None:
            append_security_rag_event(
                trace_store,
                run_state,
                event_name,
                {"source": source, **payload},
                span_id=rag_span_id if event_name in {SECURITY_RAG_STARTED, SECURITY_RAG_COMPLETED, SECURITY_RAG_FAILED} else None,
                parent_span_id=(
                    trace_parent_span_id
                    if event_name in {SECURITY_RAG_STARTED, SECURITY_RAG_COMPLETED, SECURITY_RAG_FAILED}
                    else rag_span_id
                ),
                step=reasoning_step,
            )

        append_runtime_event(SECURITY_RAG_STARTED, {
            "query": current_request,
            "entrypoint": "context_builder",
        })
        plan = None
        try:
            route_timer = Timer()
            router_trace_events: list[dict[str, Any]] = []

            def capture_router_trace(item: dict[str, Any]) -> None:
                if not isinstance(item, dict):
                    return
                event_name = str(item.get("event") or SECURITY_RAG_SEARCH_COMPLETED)
                payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
                router_trace_events.append(dict(payload))
                append_runtime_event(event_name, dict(payload))

            if hasattr(self.security_retrieval_router, "route_with_retrieval"):
                plan = self.security_retrieval_router.route_with_retrieval(
                    current_request,
                    index=self.security_knowledge_index,
                    llm_classifier=self.security_route_classifier,
                    trace_callback=capture_router_trace,
                )
                decision = plan.decision
                hits = plan.hits
                route_action = getattr(plan, "action", "")
                search_trace = {
                    "router_searches": [
                        record.__dict__ for record in getattr(plan, "searches", [])
                    ],
                    "runtime_events": router_trace_events,
                }
            else:
                decision = self.security_retrieval_router.route(
                    current_request,
                    llm_classifier=self.security_route_classifier,
                )
                hits = []
                route_action = ""
                search_trace = {}
            route_ms = route_timer.ms()
        except Exception as exc:
            append_runtime_event(SECURITY_RAG_FAILED, {
                "query": current_request,
                "stage": "route",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "latency_ms": {"total": total_timer.ms()},
            })
            write_rag_trace_if_enabled(make_rag_trace(
                source="context_auto",
                query=current_request,
                latency_ms={"total": total_timer.ms()},
                error=f"router_error:{type(exc).__name__}: {exc}",
            ))
            return (
                "<security_knowledge status=\"router_error\">\n"
                f"{type(exc).__name__}: {exc}\n"
                "</security_knowledge>",
                None,
                [],
            )
        if not getattr(decision, "use_rag", False):
            append_runtime_event(SECURITY_RAG_COMPLETED, rag_completed_payload(
                source=source,
                query=current_request,
                rewritten_query=getattr(decision, "query", current_request),
                decision=decision,
                action=route_action or "no_rag",
                reason=getattr(decision, "reason", "") or "Router skipped security RAG.",
                hits=[],
                latency_ms={"route_with_retrieval": route_ms, "total": total_timer.ms()},
                searches=getattr(plan, "searches", []) if plan is not None else [],
            ))
            write_rag_trace_if_enabled(make_rag_trace(
                source="context_auto",
                query=current_request,
                rewritten_query=getattr(decision, "query", current_request),
                router_decision=decision,
                latency_ms={"route_with_retrieval": route_ms, "total": total_timer.ms()},
            ))
            return "", decision, []
        if not hits:
            try:
                search_timer = Timer()
                legacy_search_trace: dict[str, Any] = {}
                hits = self.security_knowledge_index.search(
                    query=decision.query,
                    top_k=getattr(decision, "top_k", 5) or 5,
                    min_score=getattr(decision, "min_score", 0.0) or 0.0,
                    trace_callback=legacy_search_trace.update,
                )
                search_trace["legacy_search_ms"] = search_timer.ms()
                search_trace.update(legacy_search_trace)
                append_runtime_event(SECURITY_RAG_SEARCH_COMPLETED, search_trace_payload(
                    source=source,
                    query=decision.query,
                    stage="legacy_search",
                    retrieval_mode=str(legacy_search_trace.get("retrieval_mode") or ""),
                    top_k=getattr(decision, "top_k", 5) or 5,
                    min_score=getattr(decision, "min_score", 0.0) or 0.0,
                    hit_count=len(hits),
                    trace=legacy_search_trace,
                ))
            except Exception as exc:
                append_runtime_event(SECURITY_RAG_FAILED, {
                    "query": current_request,
                    "rewritten_query": getattr(decision, "query", current_request),
                    "stage": "search",
                    "route": getattr(decision, "route", ""),
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "latency_ms": {"route": route_ms, "total": total_timer.ms()},
                })
                write_rag_trace_if_enabled(make_rag_trace(
                    source="context_auto",
                    query=current_request,
                    rewritten_query=getattr(decision, "query", current_request),
                    router_decision=decision,
                    latency_ms={"route": route_ms, "total": total_timer.ms()},
                    error=f"search_error:{type(exc).__name__}: {exc}",
                ))
                return (
                    "<security_knowledge status=\"search_error\">\n"
                    f"route={getattr(decision, 'route', '')} query={getattr(decision, 'query', '')}\n"
                    f"{type(exc).__name__}: {exc}\n"
                    "</security_knowledge>",
                    decision,
                    [],
                )
        append_runtime_event(SECURITY_RAG_COMPLETED, rag_completed_payload(
            source=source,
            query=current_request,
            rewritten_query=decision.query,
            decision=decision,
            action=route_action,
            reason=getattr(plan, "reason", "") if plan is not None else "",
            hits=hits,
            latency_ms={
                "route_with_retrieval": route_ms,
                **(search_trace.get("latency_ms") or {}),
                "total": total_timer.ms(),
            },
            searches=getattr(plan, "searches", []) if plan is not None else [],
        ))
        write_rag_trace_if_enabled(make_rag_trace(
            source="context_auto",
            query=current_request,
            rewritten_query=decision.query,
            router_decision=decision,
            hits=hits,
            latency_ms={
                "route_with_retrieval": route_ms,
                **(search_trace.get("latency_ms") or {}),
                "total": total_timer.ms(),
            },
        ))
        if not hits:
            return "", decision, []

        lines = [
            "<security_knowledge>",
            "Use these local code-security knowledge snippets as evidence. Prefer cited source paths when answering.",
            f"route={decision.route} action={route_action} confidence={decision.confidence:.4f} query={decision.query}",
        ]
        for index, hit in enumerate(hits, start=1):
            lines.append(
                f"[{index}] [{_score_tier(hit.score)}] score={hit.score:.4f} "
                f"source={hit.source_relpath} title={hit.title}\n"
                f"{hit.text.strip()}"
            )
        lines.append("</security_knowledge>")
        return "\n\n".join(lines), decision, hits



def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content or "")


def _score_tier(score: float) -> str:
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if value >= 0.80:
        return "HIGH"
    if value >= 0.60:
        return "MEDIUM"
    return "LOW"
