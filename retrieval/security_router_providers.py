from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import time

from .security_router_models import (
    QueryRewriteProvider,
    RetrievalDecision,
    RewriteRequest,
    RewriteResult,
)
from .security_router_utils import (
    _dedupe_ordered,
    _llm_classifier_prompt,
    _llm_rewrite_prompt,
    _float,
    _normalize_query,
    _parse_json_object,
)


class LlmSecurityRouteClassifier:
    def __init__(
        self,
        *,
        runner,
        spec,
        accept_threshold: float,
        default_top_k: int,
        min_score: float,
        max_tokens: int = 350,
    ) -> None:
        self.runner = runner
        self.spec = spec
        self.accept_threshold = float(accept_threshold)
        self.default_top_k = max(1, int(default_top_k))
        self.min_score = float(min_score)
        self.max_tokens = max(1, int(max_tokens))

    def __call__(
        self,
        query: str,
        *,
        embedding_score: float,
        matched_intent: str,
    ) -> RetrievalDecision:
        prompt = _llm_classifier_prompt(
            query=query,
            embedding_score=embedding_score,
            matched_intent=matched_intent,
        )
        try:
            content = self.runner.run(
                spec=self.spec,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a retrieval router. Return only a strict "
                            "JSON object with no markdown."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=self.max_tokens,
            )
        except Exception as exc:
            return RetrievalDecision(
                use_rag=False,
                target=None,
                query=query,
                confidence=float(embedding_score or 0.0),
                reason=f"LLM route classifier failed: {type(exc).__name__}: {exc}",
                route="llm_error",
            )

        payload = _parse_json_object(content)
        confidence = _float(payload.get("confidence"), 0.0)
        use_rag = bool(payload.get("needs_retrieval")) and confidence >= self.accept_threshold
        rewritten = str(payload.get("query") or query).strip() or query
        return RetrievalDecision(
            use_rag=use_rag,
            target="security_kb" if use_rag else None,
            query=rewritten,
            confidence=confidence,
            reason=str(payload.get("reason") or "LLM classified retrieval need."),
            route="llm",
            embedding_score=float(embedding_score or 0.0),
            matched_intent=matched_intent,
            top_k=self.default_top_k,
            min_score=self.min_score,
        )


class RuleBasedQueryRewriteProvider:
    def __init__(self, router: "SecurityRetrievalRouter") -> None:
        self.router = router

    def rewrite(self, request: RewriteRequest) -> RewriteResult:
        if request.mode == "decompose":
            queries = self.router._rule_based_decompose_query(request.query, request=request)
            return RewriteResult(
                query=queries[0] if queries else request.query,
                queries=queries,
                reason="Rule-based deterministic query decomposition.",
                provider="rule",
            )
        query = self.router._rule_based_lightweight_expansion(request.query, request=request)
        return RewriteResult(
            query=query,
            queries=[query] if query else [],
            reason="Rule-based deterministic query expansion.",
            provider="rule",
        )


class LLMQueryRewriteProvider:
    def __init__(
        self,
        *,
        runner,
        spec,
        fallback: QueryRewriteProvider,
        max_tokens: int = 260,
        max_queries: int = 4,
        cache_max_size: int = 512,
        cache_ttl_seconds: int = 3600,
    ) -> None:
        self.runner = runner
        self.spec = spec
        self.fallback = fallback
        self.max_tokens = max(1, int(max_tokens))
        self.max_queries = max(1, int(max_queries))
        self.cache_max_size = max(0, int(cache_max_size))
        self.cache_ttl_seconds = max(0, int(cache_ttl_seconds))
        self._cache: OrderedDict[str, tuple[float, RewriteResult]] = OrderedDict()

    def rewrite(self, request: RewriteRequest) -> RewriteResult:
        cache_key = self._cache_key(request)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return _copy_rewrite_result(
                cached,
                metadata={"rewrite_cache_hit": "true"},
                latency_ms={"cache": 0.0, "total": 0.0},
            )
        total_started = time.perf_counter()
        fallback_started = time.perf_counter()
        fallback_result = self.fallback.rewrite(request)
        fallback_ms = _elapsed_ms(fallback_started)
        prompt = _llm_rewrite_prompt(request=request, fallback=fallback_result, max_queries=self.max_queries)
        try:
            llm_started = time.perf_counter()
            content = self.runner.run(
                spec=self.spec,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You rewrite code-security RAG search queries. "
                            "Return only a strict JSON object with no markdown."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=self.max_tokens,
            )
            llm_ms = _elapsed_ms(llm_started)
        except Exception as exc:
            result = _fallback_rewrite_result(
                fallback_result,
                reason=f"LLM rewrite failed with {type(exc).__name__}; using fallback.",
                metadata={
                    "llm_raw_content": "",
                    "llm_error_type": type(exc).__name__,
                    "llm_error_message": str(exc),
                },
            )
            return self._cache_put_and_return(
                cache_key,
                _copy_rewrite_result(
                    result,
                    metadata={"rewrite_cache_hit": "false"},
                    latency_ms={
                        "fallback": fallback_ms,
                        "llm": _elapsed_ms(llm_started) if "llm_started" in locals() else 0.0,
                        "total": _elapsed_ms(total_started),
                    },
                ),
            )
        if not str(content or "").strip():
            result = _fallback_rewrite_result(
                fallback_result,
                reason="LLM rewrite returned empty content; using fallback.",
                metadata={"llm_raw_content": str(content or "")},
            )
            return self._cache_put_and_return(
                cache_key,
                _copy_rewrite_result(
                    result,
                    metadata={"rewrite_cache_hit": "false"},
                    latency_ms={"fallback": fallback_ms, "llm": llm_ms, "total": _elapsed_ms(total_started)},
                ),
            )
        payload = _parse_json_object(content)
        if not payload:
            result = _fallback_rewrite_result(
                fallback_result,
                reason="LLM rewrite returned non-JSON content; using fallback.",
                metadata={"llm_raw_content": str(content or "")},
            )
            return self._cache_put_and_return(
                cache_key,
                _copy_rewrite_result(
                    result,
                    metadata={"rewrite_cache_hit": "false"},
                    latency_ms={"fallback": fallback_ms, "llm": llm_ms, "total": _elapsed_ms(total_started)},
                ),
            )
        query = _normalize_query(payload.get("query") or "")
        raw_queries = payload.get("queries")
        queries = []
        if isinstance(raw_queries, list):
            queries = [_normalize_query(item) for item in raw_queries if _normalize_query(item)]
        if query:
            queries.insert(0, query)
        queries = _dedupe_ordered(queries)[: self.max_queries]
        if not queries:
            result = _fallback_rewrite_result(
                fallback_result,
                reason="LLM rewrite JSON did not include usable queries; using fallback.",
                metadata={"llm_raw_content": str(content or "")},
            )
            return self._cache_put_and_return(
                cache_key,
                _copy_rewrite_result(
                    result,
                    metadata={"rewrite_cache_hit": "false"},
                    latency_ms={"fallback": fallback_ms, "llm": llm_ms, "total": _elapsed_ms(total_started)},
                ),
            )
        result = RewriteResult(
            query=queries[0],
            queries=queries,
            reason=str(payload.get("reason") or "LLM generated retrieval query rewrite."),
            provider="llm",
            metadata={
                "fallback_provider": fallback_result.provider,
                "llm_raw_content": str(content or ""),
                "rewrite_cache_hit": "false",
            },
            latency_ms={"fallback": fallback_ms, "llm": llm_ms, "total": _elapsed_ms(total_started)},
        )
        return self._cache_put_and_return(cache_key, result)

    def _cache_key(self, request: RewriteRequest) -> str:
        raw = json.dumps(
            {
                "query": request.query,
                "mode": request.mode,
                "max_queries": self.max_queries,
                "purpose": getattr(self.spec, "model_purpose", "") or "",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> RewriteResult | None:
        if self.cache_max_size <= 0 or self.cache_ttl_seconds <= 0:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, result = entry
        if time.monotonic() - ts > self.cache_ttl_seconds:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return result

    def _cache_put_and_return(self, key: str, result: RewriteResult) -> RewriteResult:
        if self.cache_max_size <= 0 or self.cache_ttl_seconds <= 0:
            return result
        self._cache[key] = (time.monotonic(), result)
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_max_size:
            self._cache.popitem(last=False)
        return result


def _fallback_rewrite_result(
    fallback: RewriteResult,
    *,
    reason: str,
    metadata: dict[str, str] | None = None,
) -> RewriteResult:
    return RewriteResult(
        query=fallback.query,
        queries=list(fallback.queries),
        reason=f"{reason} {fallback.reason}".strip(),
        provider=fallback.provider,
        metadata={
            **fallback.metadata,
            "fallback_reason": reason,
            "llm_rewrite_used": "false",
            **(metadata or {}),
        },
        latency_ms=dict(fallback.latency_ms),
    )


def _copy_rewrite_result(
    result: RewriteResult,
    *,
    metadata: dict[str, str] | None = None,
    latency_ms: dict[str, float] | None = None,
) -> RewriteResult:
    return RewriteResult(
        query=result.query,
        queries=list(result.queries),
        reason=result.reason,
        provider=result.provider,
        metadata={**result.metadata, **(metadata or {})},
        latency_ms={**result.latency_ms, **(latency_ms or {})},
    )


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000
