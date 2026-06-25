from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re
import time
from typing import Any, Callable

from memory.embeddings import EmbeddingProvider

from .security_router_defaults import (
    DEFAULT_BLOCK_PATTERNS,
    DEFAULT_SECURITY_INTENTS,
    DEFAULT_SECURITY_KEYWORDS,
    DEFAULT_TOPIC_EXPANSIONS,
)
from .security_router_models import (
    LLMRouteClassifier,
    QueryRewriteProvider,
    RetrievalDecision,
    RetrievalSearchRecord,
    RoutedRetrievalPlan,
    RewriteRequest,
    RewriteResult,
    SecurityRouteConfig,
)
from .security_router_providers import RuleBasedQueryRewriteProvider
from .security_router_utils import (
    _clarification_for_query,
    _cosine,
    _dedupe_ordered,
    _dedupe_words,
    _int,
    _is_number,
    _normalize_query,
    _summarize_hits_for_rewrite,
)


class SecurityRetrievalRouter:
    def __init__(
        self,
        *,
        embeddings: EmbeddingProvider,
        config: SecurityRouteConfig | None = None,
        keywords: dict[str, str] | None = None,
        intents: list[str] | None = None,
        block_patterns: list[tuple[str, re.Pattern, str]] | None = None,
        topic_expansions: list[tuple[re.Pattern, str]] | None = None,
        rewrite_provider: QueryRewriteProvider | None = None,
    ) -> None:
        self.embeddings = embeddings
        self.config = config or SecurityRouteConfig()
        self.keywords = DEFAULT_SECURITY_KEYWORDS if keywords is None else keywords
        self.intents = DEFAULT_SECURITY_INTENTS if intents is None else intents
        self.block_patterns = DEFAULT_BLOCK_PATTERNS if block_patterns is None else block_patterns
        self.topic_expansions = DEFAULT_TOPIC_EXPANSIONS if topic_expansions is None else topic_expansions
        self.rewrite_provider = rewrite_provider
        self._intent_vectors: list[list[float]] | None = None

    def route(
        self,
        query: str,
        *,
        llm_classifier: LLMRouteClassifier | None = None,
    ) -> RetrievalDecision:
        query = _normalize_query(query)
        if not query:
            return self._no_rag(query, route="empty", reason="Empty query.")

        blocked = self._blocked_decision(query)
        if blocked is not None:
            return blocked

        keyword_matches = self._keyword_matches(query)
        score, matched_intent = self.intent_similarity(query)
        if keyword_matches:
            rewritten = self.rewrite_query(query, keyword_matches=keyword_matches)
            return RetrievalDecision(
                use_rag=True,
                target="security_kb",
                query=rewritten,
                confidence=0.90,
                reason="Matched explicit security keyword.",
                route="keyword",
                keyword_matches=keyword_matches,
                embedding_score=score,
                matched_intent=matched_intent,
                top_k=self.config.default_top_k,
                min_score=self.config.min_score,
            )

        if score >= self.config.high_threshold:
            return RetrievalDecision(
                use_rag=True,
                target="security_kb",
                query=self.rewrite_query(query, matched_intent=matched_intent, intent_score=score),
                confidence=score,
                reason="Matched security intent by embedding similarity.",
                route="embedding_high",
                embedding_score=score,
                matched_intent=matched_intent,
                top_k=self.config.default_top_k,
                min_score=self.config.min_score,
            )

        if score <= self.config.low_threshold:
            return RetrievalDecision(
                use_rag=True,
                target="security_kb",
                query=query,
                confidence=score,
                reason="Security intent similarity is low, but no negative gate matched; try fast dense retrieval.",
                route="fast_dense_low",
                embedding_score=score,
                matched_intent=matched_intent,
                llm_required=True,
                top_k=self.config.default_top_k,
                min_score=self.config.min_score,
            )

        if llm_classifier is not None:
            decision = llm_classifier(query, embedding_score=score, matched_intent=matched_intent)
            if decision.use_rag:
                return RetrievalDecision(
                    use_rag=True,
                    target=decision.target or "security_kb",
                    query=decision.query or self.rewrite_query(query, matched_intent=matched_intent, intent_score=score),
                    confidence=decision.confidence,
                    reason=decision.reason or "LLM classifier accepted retrieval.",
                    route="llm",
                    embedding_score=score,
                    matched_intent=matched_intent,
                    top_k=decision.top_k or self.config.default_top_k,
                    min_score=decision.min_score,
                )
            return RetrievalDecision(
                use_rag=False,
                target=None,
                query=query,
                confidence=decision.confidence,
                reason=decision.reason or "LLM classifier rejected retrieval.",
                route=decision.route or "llm",
                embedding_score=score,
                matched_intent=matched_intent,
            )

        return RetrievalDecision(
            use_rag=True,
            target="security_kb",
            query=query,
            confidence=score,
            reason="Ambiguous security intent; default to fast dense retrieval before expensive routing.",
            route="fast_dense_default",
            embedding_score=score,
            matched_intent=matched_intent,
            llm_required=True,
            top_k=self.config.default_top_k,
            min_score=self.config.min_score,
        )

    def route_with_retrieval(
        self,
        query: str,
        *,
        index,
        llm_classifier: LLMRouteClassifier | None = None,
        top_k: int | None = None,
        min_score: float | None = None,
        use_cache: bool = True,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> RoutedRetrievalPlan:
        """Route a query with cheap retrieval evidence before escalating.

        Pipeline:
        1. Block unsafe/out-of-scope/insufficient-evidence prompts.
        2. Run fast dense retrieval by default.
        3. Use retrieval confidence to decide direct/rewrite/hybrid/reranker/abstain.
        4. Call LLM router only for ambiguous evidence.
        """

        original_query = _normalize_query(query)
        if not original_query:
            empty = self._no_rag(original_query, route="empty", reason="Empty query.")
            return RoutedRetrievalPlan(
                decision=empty,
                hits=[],
                action="abstain",
                reason=empty.reason,
            )

        blocked = self._blocked_decision(original_query)
        if blocked is not None:
            action = "ask_clarification" if blocked.route == "insufficient_evidence" else "abstain"
            return RoutedRetrievalPlan(
                decision=blocked,
                hits=[],
                action=action,
                reason=blocked.clarification or blocked.reason,
            )

        base_decision = self.route(original_query, llm_classifier=None)
        base_decision = self._with_route(base_decision, query=original_query)
        top_k = max(1, int(top_k or base_decision.top_k or self.config.default_top_k))
        min_score = self.config.min_score if min_score is None else float(min_score)
        searches: list[RetrievalSearchRecord] = []
        rewrites: list[RewriteResult] = []
        dense_query = original_query
        dense_hits: list | None = None
        pre_dense_provider = str(self.config.pre_dense_rewrite_provider).lower()
        if self.config.pre_dense_rewrite_enabled:
            if pre_dense_provider != "rule" and self.config.pre_dense_parallel_enabled:
                original_dense_records: list[RetrievalSearchRecord] = []
                with ThreadPoolExecutor(max_workers=2) as executor:
                    dense_future = executor.submit(
                        self._search,
                        index,
                        query=original_query,
                        top_k=top_k,
                        min_score=min_score,
                        mode="dense",
                        use_reranker=False,
                        use_cache=use_cache,
                        stage="fast_dense",
                        records=original_dense_records,
                        trace_callback=trace_callback,
                    )
                    rewrite_future = executor.submit(
                        self.rewrite_with_provider,
                        original_query,
                        mode="pre_dense",
                        decision=base_decision,
                    )
                    rewrite = rewrite_future.result()
                    dense_hits = dense_future.result()
                    searches.extend(original_dense_records)
            elif pre_dense_provider == "rule":
                rewrite_started = time.perf_counter()
                rewrite = RuleBasedQueryRewriteProvider(self).rewrite(
                    self._rewrite_request(
                        original_query,
                        mode="pre_dense",
                        decision=base_decision,
                    )
                )
                rewrite = RewriteResult(
                    query=rewrite.query,
                    queries=list(rewrite.queries),
                    reason=rewrite.reason,
                    provider=rewrite.provider,
                    metadata=dict(rewrite.metadata),
                    latency_ms={**rewrite.latency_ms, "provider_total": (time.perf_counter() - rewrite_started) * 1000},
                )
            else:
                rewrite = self.rewrite_with_provider(
                    original_query,
                    mode="pre_dense",
                    decision=base_decision,
                )
            rewrites.append(rewrite)
            if rewrite.query and rewrite.query != original_query:
                dense_query = rewrite.query
                dense_hits = None
                base_decision = self._with_route(
                    base_decision,
                    query=dense_query,
                    route=f"{base_decision.route}:pre_dense_{rewrite.provider}",
                    reason=f"{rewrite.provider} pre-dense rewrite applied before fast dense retrieval.",
                )

        if dense_hits is None:
            dense_hits = self._search(
                index,
                query=dense_query,
                top_k=top_k,
                min_score=min_score,
                mode="dense",
                use_reranker=False,
                use_cache=use_cache,
                stage="fast_dense",
                records=searches,
                trace_callback=trace_callback,
            )
        dense_analysis = self._analyze_hits(dense_hits)
        dense_tier = self.confidence_tier(dense_analysis)
        dense_confirmation_hits: list | None = None
        dense_confirmation_is_cross_mode = False
        if dense_tier == "high":
            if self._should_check_cross_mode(index):
                dense_confirmation_hits = self._search(
                    index,
                    query=dense_query,
                    top_k=top_k,
                    min_score=min_score,
                    mode="hybrid",
                    use_reranker=False,
                    use_cache=use_cache,
                    stage="hybrid_confirmation",
                    records=searches,
                    trace_callback=trace_callback,
                )
                dense_confirmation_is_cross_mode = self._search_record_used_cross_mode(searches[-1])
            dense_gate = self._direct_evidence_gate(
                dense_analysis,
                dense_hits,
                comparison_hits=dense_confirmation_hits if dense_confirmation_is_cross_mode else None,
            )
            if not dense_gate["passed"]:
                dense_tier = "medium"
            else:
                gate_reason = self._format_evidence_gate(dense_gate)
                return RoutedRetrievalPlan(
                    decision=self._with_route(
                        base_decision,
                        route=f"{base_decision.route}:dense_direct",
                        confidence=max(base_decision.confidence, dense_analysis["top_score"]),
                        reason=f"Fast dense retrieval passed direct evidence gate. {gate_reason}",
                    ),
                    hits=dense_hits,
                    action="direct",
                    reason=f"top1 high, margin sufficient, and evidence consistent. {gate_reason}",
                    searches=searches,
                    rewrites=rewrites,
                )

        if self._is_complex_or_multihop(original_query):
            return self._route_complex_query(
                original_query,
                base_decision=base_decision,
                index=index,
                top_k=top_k,
                min_score=min_score,
                use_cache=use_cache,
                searches=searches,
                rewrites=rewrites,
                seed_hits=dense_hits,
                seed_analysis=dense_analysis,
                trace_callback=trace_callback,
            )

        best_hits = dense_hits
        best_action = "dense"
        best_reason = "fast dense retrieval did not meet direct evidence threshold"
        best_score = dense_analysis["top_score"]
        best_query = dense_query
        if dense_confirmation_hits and dense_confirmation_is_cross_mode:
            confirmation_analysis = self._analyze_hits(dense_confirmation_hits)
            if confirmation_analysis["top_score"] > best_score:
                best_hits = dense_confirmation_hits
                best_score = confirmation_analysis["top_score"]
                best_action = "hybrid"
                best_reason = "hybrid confirmation improved dense evidence"
                best_query = dense_query

        if self._needs_hybrid(original_query):
            hybrid_is_cross_mode = False
            if dense_confirmation_hits is not None and dense_confirmation_is_cross_mode and original_query == dense_query:
                hybrid_hits = dense_confirmation_hits
                hybrid_is_cross_mode = True
            else:
                hybrid_hits = self._search(
                    index,
                    query=original_query,
                    top_k=top_k,
                    min_score=min_score,
                    mode="hybrid",
                    use_reranker=False,
                    use_cache=use_cache,
                    stage="hybrid_exact",
                    records=searches,
                    trace_callback=trace_callback,
                )
                hybrid_is_cross_mode = self._search_record_used_cross_mode(searches[-1])
            hybrid_analysis = self._analyze_hits(hybrid_hits)
            if hybrid_analysis["top_score"] > best_score:
                best_hits = hybrid_hits
                best_score = hybrid_analysis["top_score"]
                best_action = "hybrid"
                best_reason = "query needs exact keyword/identifier matching"
                best_query = original_query
            hybrid_gate = self._direct_evidence_gate(
                hybrid_analysis,
                hybrid_hits,
                comparison_hits=dense_hits if hybrid_is_cross_mode else None,
            )
            if self.confidence_tier(hybrid_analysis) == "high" and hybrid_gate["passed"]:
                gate_reason = self._format_evidence_gate(hybrid_gate)
                return RoutedRetrievalPlan(
                    decision=self._with_route(
                        base_decision,
                        route=f"{base_decision.route}:hybrid_direct",
                        confidence=max(base_decision.confidence, hybrid_analysis["top_score"]),
                        reason=f"Hybrid retrieval passed direct evidence gate for an exact-match query. {gate_reason}",
                    ),
                    hits=hybrid_hits,
                    action="hybrid",
                    reason=f"exact keyword matching improved evidence. {gate_reason}",
                    searches=searches,
                    rewrites=rewrites,
                )

        if dense_tier == "medium":
            rewrite = self.rewrite_with_provider(
                original_query,
                mode="expansion",
                decision=base_decision,
                tier=dense_tier,
                analysis=dense_analysis,
                hits=dense_hits,
            )
            rewrites.append(rewrite)
            expanded = rewrite.query
            if expanded != original_query:
                expansion_hits = self._search(
                    index,
                    query=expanded,
                    top_k=top_k,
                    min_score=min_score,
                    mode="dense",
                    use_reranker=True,
                    use_cache=use_cache,
                    stage="expansion_rerank",
                    records=searches,
                    trace_callback=trace_callback,
                )
                expansion_analysis = self._analyze_hits(expansion_hits)
                expansion_gate = self._direct_evidence_gate(
                    expansion_analysis,
                    expansion_hits,
                    original_hits=dense_hits,
                    rewrite_hits=expansion_hits,
                    reranker_hits=expansion_hits,
                )
                if (
                    expansion_analysis["top_score"] >= best_score
                    or self._should_accept_reranked_expansion(expansion_analysis, expansion_gate)
                ):
                    best_hits = expansion_hits
                    best_score = max(best_score, expansion_analysis["top_score"])
                    best_action = "expansion"
                    best_reason = f"medium confidence evidence accepted after {rewrite.provider} expansion and rerank"
                    best_query = expanded
                if self.confidence_tier(expansion_analysis) == "high" and expansion_gate["passed"]:
                    gate_reason = self._format_evidence_gate(expansion_gate)
                    return RoutedRetrievalPlan(
                        decision=self._with_route(
                            base_decision,
                            query=expanded,
                            route=f"{base_decision.route}:expansion_direct",
                            confidence=max(base_decision.confidence, expansion_analysis["top_score"]),
                            reason=(
                                f"{rewrite.provider} expansion plus forced rerank passed direct evidence gate. "
                                f"{gate_reason}"
                            ),
                        ),
                        hits=expansion_hits,
                        action="expansion",
                        reason=f"medium confidence {rewrite.provider} expansion used forced rerank. {gate_reason}",
                        searches=searches,
                        rewrites=rewrites,
                    )

        if dense_tier == "low":
            rewrite = self.rewrite_with_provider(
                original_query,
                mode="low_retry",
                decision=base_decision,
                tier=dense_tier,
                analysis=dense_analysis,
                hits=dense_hits,
            )
            rewrites.append(rewrite)
            if rewrite.query != original_query:
                rewrite_hits = self._search(
                    index,
                    query=rewrite.query,
                    top_k=top_k,
                    min_score=min_score,
                    mode="dense",
                    use_reranker=False,
                    use_cache=use_cache,
                    stage=f"{rewrite.provider}_rewrite_dense",
                    records=searches,
                    trace_callback=trace_callback,
                )
                rewrite_analysis = self._analyze_hits(rewrite_hits)
                if rewrite_analysis["top_score"] >= best_score:
                    best_hits = rewrite_hits
                    best_score = rewrite_analysis["top_score"]
                    best_action = f"{rewrite.provider}_rewrite"
                    best_reason = f"low confidence evidence improved after {rewrite.provider} rewrite retry"
                    best_query = rewrite.query
                if self.confidence_tier(rewrite_analysis) in {"high", "medium"}:
                    return RoutedRetrievalPlan(
                        decision=self._with_route(
                            base_decision,
                            query=rewrite.query,
                            route=f"{base_decision.route}:{rewrite.provider}_rewrite",
                            confidence=max(base_decision.confidence, rewrite_analysis["top_score"]),
                            reason=f"{rewrite.provider} rewrite retry produced sufficient evidence.",
                        ),
                            hits=rewrite_hits,
                            action=f"{rewrite.provider}_rewrite",
                            reason=f"low confidence retry used {rewrite.provider} rewrite",
                            searches=searches,
                            rewrites=rewrites,
                        )

            if llm_classifier is not None:
                llm_decision = llm_classifier(
                    original_query,
                    embedding_score=base_decision.embedding_score,
                    matched_intent=base_decision.matched_intent,
                )
                if llm_decision.use_rag:
                    llm_hits = self._search(
                        index,
                        query=llm_decision.query or original_query,
                        top_k=top_k,
                        min_score=min_score,
                        mode="dense",
                        use_reranker=False,
                        use_cache=use_cache,
                        stage="llm_rewrite_dense",
                        records=searches,
                        trace_callback=trace_callback,
                    )
                    llm_analysis = self._analyze_hits(llm_hits)
                    if llm_analysis["top_score"] >= best_score:
                        best_hits = llm_hits
                        best_score = llm_analysis["top_score"]
                        best_action = "llm_rewrite"
                        best_reason = "low confidence evidence improved after LLM rewrite retry"
                        best_query = llm_decision.query or original_query
                    if self.confidence_tier(llm_analysis) in {"high", "medium"}:
                        return RoutedRetrievalPlan(
                            decision=self._with_route(
                                llm_decision,
                                route="llm_rewrite",
                                confidence=max(llm_decision.confidence, llm_analysis["top_score"]),
                                reason="LLM rewrite retry produced sufficient evidence.",
                            ),
                            hits=llm_hits,
                            action="llm_rewrite",
                            reason="low confidence retry used LLM rewrite",
                            searches=searches,
                            rewrites=rewrites,
                            llm_decision=llm_decision,
                        )
                elif not best_hits or best_score < self.config.retrieval_low_threshold:
                    return self._ask_or_abstain(
                        original_query,
                        base_decision=llm_decision,
                        reason=llm_decision.reason or "LLM rewrite fallback rejected retrieval.",
                        searches=searches,
                        rewrites=rewrites,
                        ask=False,
                    )

            best_analysis = self._analyze_hits(best_hits)
            if self.confidence_tier(best_analysis) == "low" and self._needs_reranker(best_analysis):
                rerank_hits = self._search(
                    index,
                    query=best_query,
                    top_k=top_k,
                    min_score=min_score,
                    mode="dense",
                    use_reranker=True,
                    use_cache=use_cache,
                    stage="rerank",
                    records=searches,
                    trace_callback=trace_callback,
                )
                rerank_analysis = self._analyze_hits(rerank_hits)
                if rerank_analysis["top_score"] >= best_score:
                    best_hits = rerank_hits
                    best_score = rerank_analysis["top_score"]
                    best_action = "reranker"
                    best_reason = "low confidence evidence used reranker fallback"
                if self.confidence_tier(rerank_analysis) in {"high", "medium"}:
                    return RoutedRetrievalPlan(
                        decision=self._with_route(
                            base_decision,
                            query=best_query,
                            route=f"{base_decision.route}:reranker",
                            confidence=max(base_decision.confidence, rerank_analysis["top_score"]),
                            reason="Reranker fallback produced sufficient evidence.",
                        ),
                        hits=rerank_hits,
                        action="reranker",
                        reason="reranker fallback improved low confidence evidence",
                        searches=searches,
                        rewrites=rewrites,
                    )

        if not best_hits or best_score < self.config.retrieval_low_threshold:
            return self._ask_or_abstain(
                original_query,
                base_decision=base_decision,
                reason="Retrieval did not produce sufficient evidence.",
                searches=searches,
                rewrites=rewrites,
                ask=False,
            )

        return RoutedRetrievalPlan(
            decision=self._with_route(
                base_decision,
                query=best_query,
                route=f"{base_decision.route}:{best_action}",
                confidence=max(base_decision.confidence, best_score),
                reason=best_reason,
            ),
            hits=best_hits,
            action=best_action,
            reason=best_reason,
            searches=searches,
            rewrites=rewrites,
        )

    def _route_complex_query(
        self,
        query: str,
        *,
        base_decision: RetrievalDecision,
        index,
        top_k: int,
        min_score: float,
        use_cache: bool,
        searches: list[RetrievalSearchRecord],
        rewrites: list[RewriteResult],
        seed_hits: list,
        seed_analysis: dict[str, float],
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> RoutedRetrievalPlan:
        rewrite = self.rewrite_with_provider(
            query,
            mode="decompose",
            decision=base_decision,
            tier=self.confidence_tier(seed_analysis),
            analysis=seed_analysis,
            hits=seed_hits,
        )
        rewrites.append(rewrite)
        subqueries = _dedupe_ordered(rewrite.queries or [query])
        if not subqueries:
            subqueries = _dedupe_ordered([query])
        query_key = _normalize_query(query)
        all_hits = list(seed_hits)
        subqueries_to_search = [subquery for subquery in subqueries if subquery and subquery != query_key]
        if (
            self.config.decompose_parallel_enabled
            and len(subqueries_to_search) > 1
        ):
            workers = min(len(subqueries_to_search), max(1, int(self.config.decompose_parallel_workers)))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(
                        self._search_decompose_subquery,
                        index,
                        subquery=subquery,
                        top_k=top_k,
                        min_score=min_score,
                        use_cache=use_cache,
                        trace_callback=trace_callback,
                    )
                    for subquery in subqueries_to_search
                ]
                for future in futures:
                    sub_hits, sub_records = future.result()
                    all_hits.extend(sub_hits)
                    searches.extend(sub_records)
        else:
            for subquery in subqueries_to_search:
                sub_hits, sub_records = self._search_decompose_subquery(
                    index,
                    subquery=subquery,
                    top_k=top_k,
                    min_score=min_score,
                    use_cache=use_cache,
                    trace_callback=trace_callback,
                )
                all_hits.extend(sub_hits)
                searches.extend(sub_records)
        merged_hits = self._merge_hits(all_hits)[:top_k]
        merged_analysis = self._analyze_hits(merged_hits)
        merged_tier = self.confidence_tier(merged_analysis)
        if merged_hits:
            rerank_hits = self._search(
                index,
                query=" ".join(subqueries[:4]).strip() or query_key or query,
                top_k=top_k,
                min_score=min_score,
                mode="dense",
                use_reranker=True,
                use_cache=use_cache,
                stage="decompose_rerank",
                records=searches,
                trace_callback=trace_callback,
            )
            combined = self._merge_hits([*merged_hits, *rerank_hits])[:top_k]
            combined_analysis = self._analyze_hits(combined)
            combined_tier = self.confidence_tier(combined_analysis)
            if combined_tier in {"high", "medium"}:
                return RoutedRetrievalPlan(
                    decision=self._with_route(
                        base_decision,
                        route=f"{base_decision.route}:decompose_reranker",
                        confidence=max(base_decision.confidence, combined_analysis["top_score"]),
                        reason=f"Complex query {rewrite.provider} decomposition plus forced reranker produced sufficient evidence.",
                    ),
                    hits=combined,
                    action="decompose",
                    reason=f"complex query used {rewrite.provider} multi-query decomposition and forced reranker",
                    searches=searches,
                    rewrites=rewrites,
                )

        if merged_tier in {"high", "medium"}:
            return RoutedRetrievalPlan(
                decision=self._with_route(
                    base_decision,
                    route=f"{base_decision.route}:decompose",
                    confidence=max(base_decision.confidence, merged_analysis["top_score"]),
                    reason=f"Complex query {rewrite.provider} decomposition produced sufficient evidence.",
                ),
                hits=merged_hits,
                action="decompose",
                reason=f"complex query used {rewrite.provider} multi-query decomposition",
                searches=searches,
                rewrites=rewrites,
            )

        return self._ask_or_abstain(
            query,
            base_decision=base_decision,
            reason="Complex query decomposition did not produce sufficient evidence.",
            searches=searches,
            rewrites=rewrites,
            ask=True,
        )

    def _search_decompose_subquery(
        self,
        index,
        *,
        subquery: str,
        top_k: int,
        min_score: float,
        use_cache: bool,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[list, list[RetrievalSearchRecord]]:
        subquery = _normalize_query(subquery)
        if not subquery:
            return [], []
        sub_records: list[RetrievalSearchRecord] = []
        hits = self._search(
            index,
            query=subquery,
            top_k=top_k,
            min_score=min_score,
            mode="dense",
            use_reranker=False,
            use_cache=use_cache,
            stage="decompose_dense",
            records=sub_records,
            trace_callback=trace_callback,
        )
        return hits, sub_records

    def _search(
        self,
        index,
        *,
        query: str,
        top_k: int,
        min_score: float,
        mode: str,
        use_reranker: bool,
        use_cache: bool,
        stage: str,
        records: list[RetrievalSearchRecord],
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> list:
        query = _normalize_query(query)
        if not query:
            return []
        search_trace: dict[str, Any] = {}

        def capture_search_trace(payload: dict[str, Any]) -> None:
            if isinstance(payload, dict):
                search_trace.update(payload)

        try:
            hits = index.search(
                query,
                top_k=top_k,
                min_score=min_score,
                use_reranker=use_reranker,
                retrieval_mode=mode,
                use_cache=use_cache,
                trace_callback=capture_search_trace,
            )
        except Exception as exc:
            if trace_callback is not None:
                trace_callback({
                    "event": "security.rag.search.failed",
                    "payload": {
                        "stage": stage,
                        "query": query,
                        "retrieval_mode": mode,
                        "reranker_enabled": bool(use_reranker),
                        "top_k": top_k,
                        "min_score": min_score,
                        "use_cache": use_cache,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                })
            raise
        analysis = self._analyze_hits(hits)
        latency_ms = search_trace.get("latency_ms")
        if not isinstance(latency_ms, dict):
            latency_ms = {}
        tier = self.confidence_tier(analysis)
        record = RetrievalSearchRecord(
            stage=stage,
            query=query,
            mode=mode,
            use_reranker=use_reranker,
            hit_count=len(hits),
            tier=tier,
            top_score=analysis["top_score"],
            score_gap=analysis["score_gap"],
            source_concentration=analysis["source_concentration"],
            intent_concentration=analysis["intent_concentration"],
            cache_hit=bool(search_trace.get("cache_hit", False)),
            candidate_count=_int(search_trace.get("candidate_count"), 0),
            final_count=_int(search_trace.get("final_count"), len(hits)),
            latency_ms={
                str(name): float(value)
                for name, value in latency_ms.items()
                if _is_number(value)
            },
        )
        records.append(record)
        if trace_callback is not None:
            trace_callback({
                "event": "security.rag.search.completed",
                "payload": {
                    "stage": stage,
                    "query": query,
                    "retrieval_mode": mode,
                    "hybrid_enabled": bool(search_trace.get("hybrid_enabled", False)),
                    "reranker_enabled": bool(use_reranker),
                    "top_k": top_k,
                    "min_score": min_score,
                    "use_cache": use_cache,
                    "cache_hit": record.cache_hit,
                    "candidate_count": record.candidate_count,
                    "final_count": record.final_count,
                    "hit_count": record.hit_count,
                    "tier": tier,
                    "top_score": record.top_score,
                    "score_gap": record.score_gap,
                    "source_concentration": record.source_concentration,
                    "intent_concentration": record.intent_concentration,
                    "latency_ms": record.latency_ms,
                },
            })
        return hits

    def _analyze_hits(self, hits: list) -> dict[str, float]:
        if not hits:
            return {
                "top_score": 0.0,
                "score_gap": 0.0,
                "source_concentration": 0.0,
                "intent_concentration": 0.0,
                "hit_count": 0.0,
            }
        top_score = float(getattr(hits[0], "score", 0.0) or 0.0)
        second_score = float(getattr(hits[1], "score", 0.0) or 0.0) if len(hits) > 1 else 0.0
        top_sources = [
            self._source_bucket(hit)
            for hit in hits[: min(5, len(hits))]
            if self._source_bucket(hit)
        ]
        concentration = 1.0
        if top_sources:
            concentration = max(top_sources.count(source) for source in set(top_sources)) / len(top_sources)
        top_intents = [
            self._hit_intent_bucket(hit)
            for hit in hits[: min(5, len(hits))]
            if self._hit_intent_bucket(hit)
        ]
        intent_concentration = 0.0
        if top_intents:
            intent_concentration = max(top_intents.count(intent) for intent in set(top_intents)) / len(top_intents)
        return {
            "top_score": top_score,
            "score_gap": max(0.0, top_score - second_score),
            "source_concentration": concentration,
            "intent_concentration": intent_concentration,
            "hit_count": float(len(hits)),
        }

    def confidence_tier(self, analysis: dict[str, float]) -> str:
        top = float(analysis.get("top_score", 0.0) or 0.0)
        gap = float(analysis.get("score_gap", 0.0) or 0.0)
        concentration = float(analysis.get("source_concentration", 0.0) or 0.0)
        intent_concentration = float(analysis.get("intent_concentration", 0.0) or 0.0)
        if top >= self.config.retrieval_direct_threshold and (
            gap >= self.config.retrieval_gap_threshold
            or concentration >= self.config.retrieval_concentration_threshold
            or intent_concentration >= self.config.retrieval_intent_concentration_threshold
        ):
            return "high"
        if top >= self.config.retrieval_medium_threshold:
            return "medium"
        return "low"

    def _direct_evidence_gate(
        self,
        analysis: dict[str, float],
        hits: list,
        *,
        comparison_hits: list | None = None,
        original_hits: list | None = None,
        rewrite_hits: list | None = None,
        reranker_hits: list | None = None,
    ) -> dict[str, Any]:
        top = float(analysis.get("top_score", 0.0) or 0.0)
        gap = float(analysis.get("score_gap", 0.0) or 0.0)
        intent_concentration = float(analysis.get("intent_concentration", 0.0) or 0.0)
        source_concentration = float(analysis.get("source_concentration", 0.0) or 0.0)
        signals: dict[str, bool] = {
            "top1_score_high": top >= self.config.retrieval_direct_threshold,
            "top1_margin_high": gap >= self.config.retrieval_gap_threshold,
            "topk_intent_concentrated": (
                intent_concentration >= self.config.retrieval_intent_concentration_threshold
                or source_concentration >= self.config.retrieval_concentration_threshold
            ),
        }
        metrics: dict[str, float] = {
            "top_score": top,
            "score_gap": gap,
            "intent_concentration": intent_concentration,
            "source_concentration": source_concentration,
        }

        if comparison_hits is not None:
            overlap = self._hit_overlap_ratio(hits, comparison_hits)
            metrics["cross_mode_overlap"] = overlap
            signals["dense_sparse_overlap"] = (
                self._same_top_doc(hits, comparison_hits)
                or overlap >= self.config.retrieval_cross_mode_overlap_threshold
            )

        if rewrite_hits is not None:
            baseline_hits = original_hits if original_hits is not None else hits
            overlap = self._hit_overlap_ratio(baseline_hits, rewrite_hits)
            metrics["rewrite_overlap"] = overlap
            signals["rewrite_top_doc_stable"] = (
                self._same_top_doc(baseline_hits, rewrite_hits)
                or overlap >= self.config.retrieval_rewrite_overlap_threshold
            )

        if reranker_hits is not None:
            reranker_top = float(getattr(reranker_hits[0], "score", 0.0) or 0.0) if reranker_hits else 0.0
            metrics["reranker_top_score"] = reranker_top
            signals["reranker_top1_score_high"] = reranker_top >= self.config.retrieval_reranker_direct_threshold

        consistency_keys = [
            "topk_intent_concentrated",
            "dense_sparse_overlap",
            "rewrite_top_doc_stable",
            "reranker_top1_score_high",
        ]
        votes = sum(1 for key in consistency_keys if signals.get(key))
        if not self.config.retrieval_consistency_gate_enabled:
            passed = self.confidence_tier(analysis) == "high"
        else:
            passed = (
                signals["top1_score_high"]
                and signals["top1_margin_high"]
                and votes >= max(0, int(self.config.retrieval_min_consistency_votes))
            )
        return {
            "passed": passed,
            "votes": votes,
            "required_votes": max(0, int(self.config.retrieval_min_consistency_votes)),
            "signals": signals,
            "metrics": metrics,
        }

    def _format_evidence_gate(self, gate: dict[str, Any]) -> str:
        signals = gate.get("signals") if isinstance(gate.get("signals"), dict) else {}
        active = [name for name, enabled in signals.items() if enabled]
        return f"evidence_votes={gate.get('votes', 0)}/{gate.get('required_votes', 0)} signals={','.join(active) or 'none'}"

    def _should_accept_reranked_expansion(self, analysis: dict[str, float], gate: dict[str, Any]) -> bool:
        signals = gate.get("signals") if isinstance(gate.get("signals"), dict) else {}
        votes = int(gate.get("votes", 0) or 0)
        required_votes = max(0, int(gate.get("required_votes", 0) or 0))
        if signals.get("reranker_top1_score_high") and votes >= required_votes:
            return True
        return self.confidence_tier(analysis) in {"high", "medium"} and votes >= max(1, required_votes)

    def _has_direct_evidence(self, analysis: dict[str, float]) -> bool:
        return self.confidence_tier(analysis) == "high"

    def _needs_rewrite(self, analysis: dict[str, float]) -> bool:
        return self.confidence_tier(analysis) == "medium"

    def _needs_reranker(self, analysis: dict[str, float]) -> bool:
        return (
            analysis["hit_count"] >= max(2, float(self.config.retrieval_rerank_min_hits))
            and self.confidence_tier(analysis) == "low"
            and analysis["top_score"] >= self.config.retrieval_low_threshold
        )

    def _needs_llm_fallback(self, query: str, hits: list, action: str) -> bool:
        if self._is_complex_or_multihop(query):
            return True
        analysis = self._analyze_hits(hits)
        return action in {"dense", "expansion", "hybrid"} and self.confidence_tier(analysis) == "low"

    def lightweight_expansion(self, query: str, *, decision: RetrievalDecision) -> str:
        return self.rewrite_with_provider(query, mode="expansion", decision=decision).query

    def decompose_query(self, query: str, *, decision: RetrievalDecision) -> list[str]:
        result = self.rewrite_with_provider(query, mode="decompose", decision=decision)
        return result.queries or [query]

    def rewrite_with_provider(
        self,
        query: str,
        *,
        mode: str,
        decision: RetrievalDecision,
        tier: str = "",
        analysis: dict[str, float] | None = None,
        hits: list | None = None,
    ) -> RewriteResult:
        request = self._rewrite_request(
            query,
            mode=mode,
            decision=decision,
            tier=tier,
            analysis=analysis,
            hits=hits,
        )
        provider = self.rewrite_provider or RuleBasedQueryRewriteProvider(self)
        provider_started = time.perf_counter()
        try:
            result = provider.rewrite(request)
        except Exception:
            result = RuleBasedQueryRewriteProvider(self).rewrite(request)
        provider_ms = (time.perf_counter() - provider_started) * 1000
        queries = _dedupe_ordered(result.queries or ([result.query] if result.query else []))
        query_out = _normalize_query(result.query or (queries[0] if queries else request.fallback_query or query))
        if query_out:
            queries = _dedupe_ordered([query_out, *queries])
        if not queries:
            queries = _dedupe_ordered(request.fallback_queries or [query])
        return RewriteResult(
            query=queries[0] if queries else query,
            queries=queries,
            reason=result.reason,
            provider=result.provider,
            metadata=dict(result.metadata),
            latency_ms={**result.latency_ms, "provider_total": provider_ms},
        )

    def _rewrite_request(
        self,
        query: str,
        *,
        mode: str,
        decision: RetrievalDecision,
        tier: str = "",
        analysis: dict[str, float] | None = None,
        hits: list | None = None,
    ) -> RewriteRequest:
        analysis = analysis or {}
        fallback_queries = (
            self._rule_based_decompose_query(query, request=None, decision=decision)
            if mode == "decompose"
            else [self._rule_based_lightweight_expansion(query, request=None, decision=decision)]
        )
        fallback_queries = _dedupe_ordered(fallback_queries)
        return RewriteRequest(
            query=query,
            mode=mode,
            route=decision.route,
            tier=tier,
            matched_intent=decision.matched_intent,
            embedding_score=decision.embedding_score,
            keyword_matches=list(decision.keyword_matches),
            top_score=float(analysis.get("top_score", 0.0) or 0.0),
            score_gap=float(analysis.get("score_gap", 0.0) or 0.0),
            source_concentration=float(analysis.get("source_concentration", 0.0) or 0.0),
            top_hits=_summarize_hits_for_rewrite(hits or []),
            fallback_query=fallback_queries[0] if fallback_queries else query,
            fallback_queries=fallback_queries,
        )

    def _rule_based_lightweight_expansion(
        self,
        query: str,
        *,
        request: RewriteRequest | None = None,
        decision: RetrievalDecision | None = None,
    ) -> str:
        keyword_matches = request.keyword_matches if request is not None else (decision.keyword_matches if decision else [])
        matched_intent = request.matched_intent if request is not None else (decision.matched_intent if decision else "")
        embedding_score = request.embedding_score if request is not None else (decision.embedding_score if decision else 0.0)
        force_intent = request is None or request.mode in {"expansion", "decompose"}
        rewritten = self.rewrite_query(
            query,
            keyword_matches=keyword_matches,
            matched_intent=matched_intent,
            intent_score=max(embedding_score, 0.80) if force_intent else embedding_score,
        )
        topic_expansions = self._topic_expansion_matches(query)
        if topic_expansions:
            return _dedupe_words(f"{rewritten} {' '.join(topic_expansions)}")
        return rewritten

    def _rule_based_decompose_query(
        self,
        query: str,
        *,
        request: RewriteRequest | None = None,
        decision: RetrievalDecision | None = None,
    ) -> list[str]:
        pieces = [
            item.strip(" ，,。；;:：")
            for item in re.split(r"(?:->|→|、|，|,|；|;|同时|并且|以及|和|与|组合|链)", query)
            if item.strip(" ，,。；;:：")
        ]
        output: list[str] = [query]
        expanded = self._rule_based_lightweight_expansion(query, request=request, decision=decision)
        if expanded != query:
            output.append(expanded)
        for piece in pieces:
            if len(piece) < 4:
                continue
            output.append(self._rule_based_lightweight_expansion(piece, request=request, decision=decision))
            if len(output) >= 4:
                break
        return _dedupe_ordered(output[:4])

    def _merge_hits(self, hits: list) -> list:
        by_id = {}
        for hit in hits:
            hit_id = str(getattr(hit, "id", "") or id(hit))
            old = by_id.get(hit_id)
            if old is None or float(getattr(hit, "score", 0.0) or 0.0) > float(getattr(old, "score", 0.0) or 0.0):
                by_id[hit_id] = hit
        return sorted(
            by_id.values(),
            key=lambda item: float(getattr(item, "score", 0.0) or 0.0),
            reverse=True,
        )

    def _should_check_cross_mode(self, index) -> bool:
        return (
            bool(self.config.retrieval_consistency_gate_enabled)
            and bool(getattr(index, "hybrid_enabled", False))
        )

    def _search_record_used_cross_mode(self, record: RetrievalSearchRecord) -> bool:
        return record.mode == "hybrid" and "hybrid_fallback_dense" not in record.latency_ms

    def _hit_overlap_ratio(self, left: list, right: list, *, limit: int = 5) -> float:
        left_ids = self._hit_id_set(left, limit=limit)
        right_ids = self._hit_id_set(right, limit=limit)
        if not left_ids or not right_ids:
            return 0.0
        return len(left_ids & right_ids) / max(1, min(len(left_ids), len(right_ids)))

    def _same_top_doc(self, left: list, right: list) -> bool:
        left_id = self._hit_identity(left[0]) if left else ""
        right_id = self._hit_identity(right[0]) if right else ""
        return bool(left_id and right_id and left_id == right_id)

    def _hit_id_set(self, hits: list, *, limit: int = 5) -> set[str]:
        return {
            hit_id
            for hit_id in (self._hit_identity(hit) for hit in hits[: max(1, int(limit))])
            if hit_id
        }

    def _hit_identity(self, hit) -> str:
        hit_id = str(getattr(hit, "id", "") or "").strip()
        if hit_id:
            return hit_id
        source = str(getattr(hit, "source_relpath", "") or "").strip()
        chunk = str(getattr(hit, "chunk_index", "") or "").strip()
        if source or chunk:
            return f"{source}#{chunk}"
        return ""

    def _hit_intent_bucket(self, hit) -> str:
        metadata = getattr(hit, "metadata", {}) if isinstance(getattr(hit, "metadata", None), dict) else {}
        for key in ("cwe", "cwes", "category", "rule_id", "source_type", "field"):
            value = metadata.get(key)
            normalized = self._normalize_bucket_value(value)
            if normalized:
                return f"{key}:{normalized}"
        title = self._normalize_bucket_value(getattr(hit, "title", ""))
        if title:
            return f"title:{title}"
        source = self._source_bucket(hit)
        if source:
            return f"source:{source}"
        return ""

    def _normalize_bucket_value(self, value) -> str:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                normalized = self._normalize_bucket_value(item)
                if normalized:
                    return normalized
            return ""
        text = str(value or "").strip().lower()
        if not text:
            return ""
        return re.sub(r"\s+", " ", text)[:96]

    def _ask_or_abstain(
        self,
        query: str,
        *,
        base_decision: RetrievalDecision,
        reason: str,
        searches: list[RetrievalSearchRecord],
        rewrites: list[RewriteResult] | None = None,
        ask: bool,
    ) -> RoutedRetrievalPlan:
        route = "ask_clarification" if ask else "abstain_no_evidence"
        clarification = _clarification_for_query(query) if ask else ""
        decision = self._no_rag(
            query,
            route=route,
            reason=reason,
            embedding_score=base_decision.embedding_score,
            matched_intent=base_decision.matched_intent,
            clarification=clarification,
        )
        return RoutedRetrievalPlan(
            decision=decision,
            hits=[],
            action="ask_clarification" if ask else "abstain",
            reason=clarification or reason,
            searches=searches,
            rewrites=list(rewrites or []),
        )

    def _needs_hybrid(self, query: str) -> bool:
        lowered = query.lower()
        if re.search(r"\b(cve-\d{4}-\d+|cwe-\d+|ghsa-[a-z0-9-]+)\b", lowered):
            return True
        if re.search(r"\b[a-z0-9_.-]+/[a-z0-9_.-]+\b", lowered):
            return True
        exact_markers = [
            "semgrep",
            "规则",
            "rule",
            "cve",
            "cwe",
            "ghsa",
            "依赖",
            "包",
            "版本",
            "lockfile",
            "dockerfile",
            "kubernetes",
            "terraform",
            "github actions",
        ]
        return any(marker in lowered for marker in exact_markers)

    def _is_complex_or_multihop(self, query: str) -> bool:
        lowered = query.lower()
        markers = [
            "同时",
            "组合",
            "链",
            "多跳",
            "冲突",
            "证据",
            "权衡",
            "优先级",
            "怎么判断",
            "怎么确认",
            "排查",
            "审查顺序",
            "multi",
            "conflict",
            "evidence",
            "tradeoff",
        ]
        return any(marker in lowered for marker in markers)

    def _topic_expansion_matches(self, query: str) -> list[str]:
        return _dedupe_ordered([expansion for pattern, expansion in self.topic_expansions if pattern.search(query)])

    def _source_bucket(self, hit) -> str:
        source = str(getattr(hit, "source_relpath", "") or "")
        if not source:
            return ""
        parts = source.split("/")
        return "/".join(parts[:3]) if len(parts) > 2 else source

    def _blocked_decision(self, query: str) -> RetrievalDecision | None:
        for route, pattern, reason in self.block_patterns:
            if pattern.search(query):
                return self._no_rag(
                    query,
                    route=route,
                    reason=reason,
                    embedding_score=0.95,
                    clarification=_clarification_for_query(query) if route == "insufficient_evidence" else "",
                )
        return None

    def _with_route(
        self,
        decision: RetrievalDecision,
        *,
        route: str | None = None,
        query: str | None = None,
        confidence: float | None = None,
        reason: str | None = None,
    ) -> RetrievalDecision:
        return RetrievalDecision(
            use_rag=decision.use_rag,
            target=decision.target,
            query=decision.query if query is None else query,
            confidence=decision.confidence if confidence is None else confidence,
            reason=decision.reason if reason is None else reason,
            route=decision.route if route is None else route,
            keyword_matches=list(decision.keyword_matches),
            embedding_score=decision.embedding_score,
            matched_intent=decision.matched_intent,
            llm_required=decision.llm_required,
            top_k=decision.top_k,
            min_score=decision.min_score,
            clarification=decision.clarification,
        )

    def intent_similarity(self, query: str) -> tuple[float, str]:
        query_vector = self.embeddings.embed(query)
        best_score = -1.0
        best_intent = ""
        for intent, vector in zip(self.intents, self._get_intent_vectors()):
            score = _cosine(query_vector, vector)
            if score > best_score:
                best_score = score
                best_intent = intent
        return max(0.0, best_score), best_intent

    def rewrite_query(
        self,
        query: str,
        *,
        keyword_matches: list[str] | None = None,
        matched_intent: str = "",
        intent_score: float = 0.0,
    ) -> str:
        if keyword_matches:
            keyword = keyword_matches[0]
            expansion = self.keywords.get(keyword)
            if expansion:
                return _dedupe_words(f"{query} {expansion}")
        if matched_intent and float(intent_score or 0.0) >= 0.80:
            return _dedupe_words(f"{query} {matched_intent}")
        return query

    def _keyword_matches(self, query: str) -> list[str]:
        lowered = query.lower()
        matches = []
        for keyword in self.keywords:
            if re.search(rf"(?<![a-z0-9_]){re.escape(keyword.lower())}(?![a-z0-9_])", lowered):
                matches.append(keyword)
        return matches

    def _get_intent_vectors(self) -> list[list[float]]:
        if self._intent_vectors is None:
            self._intent_vectors = [self.embeddings.embed(intent) for intent in self.intents]
        return self._intent_vectors

    def _no_rag(
        self,
        query: str,
        *,
        route: str,
        reason: str,
        embedding_score: float = 0.0,
        matched_intent: str = "",
        clarification: str = "",
    ) -> RetrievalDecision:
        return RetrievalDecision(
            use_rag=False,
            target=None,
            query=query,
            confidence=max(0.0, float(embedding_score)),
            reason=reason,
            route=route,
            embedding_score=embedding_score,
            matched_intent=matched_intent,
            clarification=clarification,
        )
