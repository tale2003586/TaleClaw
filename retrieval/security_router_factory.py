from __future__ import annotations

import os

from knowledge.security_rag import build_security_embedding_provider_from_env
from memory.embeddings import EmbeddingProvider

from .security_router_core import SecurityRetrievalRouter
from .security_router_models import LLMRouteClassifier, QueryRewriteProvider, SecurityRouteConfig
from .security_router_providers import (
    LLMQueryRewriteProvider,
    LlmSecurityRouteClassifier,
    RuleBasedQueryRewriteProvider,
)
from .security_router_utils import _env_bool, _env_float, _env_int


def build_security_retrieval_router_from_env(
    *,
    embeddings: EmbeddingProvider | None = None,
    model_pool=None,
) -> SecurityRetrievalRouter:
    config = SecurityRouteConfig(
        high_threshold=_env_float("SECURITY_RAG_ROUTE_HIGH_THRESHOLD", 0.72),
        low_threshold=_env_float("SECURITY_RAG_ROUTE_LOW_THRESHOLD", 0.45),
        llm_accept_threshold=_env_float("SECURITY_RAG_ROUTE_LLM_ACCEPT_THRESHOLD", 0.60),
        default_top_k=_env_int("SECURITY_RAG_ROUTE_TOP_K", 5),
        min_score=_env_float("SECURITY_RAG_ROUTE_MIN_SCORE", 0.0),
        retrieval_direct_threshold=_env_float("SECURITY_RAG_ROUTE_RETRIEVAL_DIRECT_THRESHOLD", 0.68),
        retrieval_medium_threshold=_env_float("SECURITY_RAG_ROUTE_RETRIEVAL_MEDIUM_THRESHOLD", 0.48),
        retrieval_low_threshold=_env_float("SECURITY_RAG_ROUTE_RETRIEVAL_LOW_THRESHOLD", 0.35),
        retrieval_gap_threshold=_env_float("SECURITY_RAG_ROUTE_RETRIEVAL_GAP_THRESHOLD", 0.08),
        retrieval_concentration_threshold=_env_float(
            "SECURITY_RAG_ROUTE_RETRIEVAL_CONCENTRATION_THRESHOLD",
            0.67,
        ),
        retrieval_rerank_min_hits=_env_int("SECURITY_RAG_ROUTE_RETRIEVAL_RERANK_MIN_HITS", 5),
        retrieval_consistency_gate_enabled=_env_bool("SECURITY_RAG_ROUTE_CONSISTENCY_GATE_ENABLED", True),
        retrieval_min_consistency_votes=_env_int("SECURITY_RAG_ROUTE_MIN_CONSISTENCY_VOTES", 1),
        retrieval_cross_mode_overlap_threshold=_env_float("SECURITY_RAG_ROUTE_CROSS_MODE_OVERLAP_THRESHOLD", 0.30),
        retrieval_rewrite_overlap_threshold=_env_float("SECURITY_RAG_ROUTE_REWRITE_OVERLAP_THRESHOLD", 0.20),
        retrieval_reranker_direct_threshold=_env_float("SECURITY_RAG_ROUTE_RERANKER_DIRECT_THRESHOLD", 0.68),
        retrieval_intent_concentration_threshold=_env_float(
            "SECURITY_RAG_ROUTE_INTENT_CONCENTRATION_THRESHOLD",
            0.60,
        ),
        decompose_parallel_enabled=_env_bool("SECURITY_RAG_DECOMPOSE_PARALLEL_ENABLED", True),
        decompose_parallel_workers=_env_int("SECURITY_RAG_DECOMPOSE_PARALLEL_WORKERS", 4),
        pre_dense_rewrite_enabled=_env_bool("SECURITY_RAG_ROUTE_PRE_DENSE_REWRITE_ENABLED", False),
        pre_dense_rewrite_provider=os.getenv("SECURITY_RAG_PRE_DENSE_REWRITE_PROVIDER", "rule").strip().lower() or "rule",
        pre_dense_parallel_enabled=_env_bool("SECURITY_RAG_PRE_DENSE_PARALLEL_ENABLED", True),
    )
    router = SecurityRetrievalRouter(
        embeddings=embeddings or build_security_embedding_provider_from_env(),
        config=config,
    )
    router.rewrite_provider = build_security_query_rewrite_provider_from_env(
        router=router,
        model_pool=model_pool,
    )
    return router


def build_security_query_rewrite_provider_from_env(
    *,
    router: SecurityRetrievalRouter,
    enabled: bool | None = None,
    model_pool=None,
) -> QueryRewriteProvider:
    fallback = RuleBasedQueryRewriteProvider(router)
    if enabled is None:
        enabled = _env_bool("SECURITY_RAG_REWRITE_LLM_ENABLED", False)
    if not enabled:
        return fallback

    from models.model_task_runner import ModelTaskRunner
    from runtime.agent_spec import AgentSpec
    if model_pool is None:
        from applications.bootstrap import get_model_pool

        model_pool = get_model_pool()

    max_tokens = _env_int("SECURITY_RAG_REWRITE_LLM_MAX_TOKENS", 256)
    purpose = os.getenv("SECURITY_RAG_REWRITE_LLM_PURPOSE", "security_router").strip() or "summary"
    max_queries = _env_int("SECURITY_RAG_REWRITE_LLM_MAX_QUERIES", 2)
    runner = ModelTaskRunner(model_pool=model_pool, default_max_tokens=max_tokens)
    spec = AgentSpec(
        name="security_rag_query_rewriter",
        profile=None,
        model_purpose=purpose,
        max_tokens=max_tokens,
    )
    return LLMQueryRewriteProvider(
        runner=runner,
        spec=spec,
        fallback=fallback,
        max_tokens=max_tokens,
        max_queries=max_queries,
        cache_max_size=_env_int("SECURITY_RAG_REWRITE_CACHE_MAX_SIZE", 512),
        cache_ttl_seconds=_env_int("SECURITY_RAG_REWRITE_CACHE_TTL_SECONDS", 3600),
    )


def build_security_route_classifier_from_env(
    *,
    config: SecurityRouteConfig | None = None,
    enabled: bool | None = None,
    model_pool=None,
) -> LLMRouteClassifier | None:
    config = config or SecurityRouteConfig(
        high_threshold=_env_float("SECURITY_RAG_ROUTE_HIGH_THRESHOLD", 0.72),
        low_threshold=_env_float("SECURITY_RAG_ROUTE_LOW_THRESHOLD", 0.45),
        llm_accept_threshold=_env_float("SECURITY_RAG_ROUTE_LLM_ACCEPT_THRESHOLD", 0.60),
        default_top_k=_env_int("SECURITY_RAG_ROUTE_TOP_K", 5),
        min_score=_env_float("SECURITY_RAG_ROUTE_MIN_SCORE", 0.0),
        retrieval_direct_threshold=_env_float("SECURITY_RAG_ROUTE_RETRIEVAL_DIRECT_THRESHOLD", 0.68),
        retrieval_medium_threshold=_env_float("SECURITY_RAG_ROUTE_RETRIEVAL_MEDIUM_THRESHOLD", 0.48),
        retrieval_low_threshold=_env_float("SECURITY_RAG_ROUTE_RETRIEVAL_LOW_THRESHOLD", 0.35),
        retrieval_gap_threshold=_env_float("SECURITY_RAG_ROUTE_RETRIEVAL_GAP_THRESHOLD", 0.08),
        retrieval_concentration_threshold=_env_float(
            "SECURITY_RAG_ROUTE_RETRIEVAL_CONCENTRATION_THRESHOLD",
            0.67,
        ),
        retrieval_rerank_min_hits=_env_int("SECURITY_RAG_ROUTE_RETRIEVAL_RERANK_MIN_HITS", 5),
        retrieval_consistency_gate_enabled=_env_bool("SECURITY_RAG_ROUTE_CONSISTENCY_GATE_ENABLED", True),
        retrieval_min_consistency_votes=_env_int("SECURITY_RAG_ROUTE_MIN_CONSISTENCY_VOTES", 1),
        retrieval_cross_mode_overlap_threshold=_env_float("SECURITY_RAG_ROUTE_CROSS_MODE_OVERLAP_THRESHOLD", 0.30),
        retrieval_rewrite_overlap_threshold=_env_float("SECURITY_RAG_ROUTE_REWRITE_OVERLAP_THRESHOLD", 0.20),
        retrieval_reranker_direct_threshold=_env_float("SECURITY_RAG_ROUTE_RERANKER_DIRECT_THRESHOLD", 0.68),
        retrieval_intent_concentration_threshold=_env_float(
            "SECURITY_RAG_ROUTE_INTENT_CONCENTRATION_THRESHOLD",
            0.60,
        ),
        decompose_parallel_enabled=_env_bool("SECURITY_RAG_DECOMPOSE_PARALLEL_ENABLED", True),
        decompose_parallel_workers=_env_int("SECURITY_RAG_DECOMPOSE_PARALLEL_WORKERS", 4),
        pre_dense_rewrite_enabled=_env_bool("SECURITY_RAG_ROUTE_PRE_DENSE_REWRITE_ENABLED", False),
        pre_dense_rewrite_provider=os.getenv("SECURITY_RAG_PRE_DENSE_REWRITE_PROVIDER", "rule").strip().lower() or "rule",
        pre_dense_parallel_enabled=_env_bool("SECURITY_RAG_PRE_DENSE_PARALLEL_ENABLED", True),
    )
    if enabled is None:
        enabled = _env_bool("SECURITY_RAG_ROUTE_LLM_ENABLED", False)
    if not enabled:
        return None

    from models.model_task_runner import ModelTaskRunner
    from runtime.agent_spec import AgentSpec
    if model_pool is None:
        from applications.bootstrap import get_model_pool

        model_pool = get_model_pool()

    max_tokens = _env_int("SECURITY_RAG_ROUTE_LLM_MAX_TOKENS", 350)
    purpose = os.getenv("SECURITY_RAG_ROUTE_LLM_PURPOSE", "summary").strip() or "summary"
    runner = ModelTaskRunner(model_pool=model_pool, default_max_tokens=max_tokens)
    spec = AgentSpec(
        name="security_rag_route_classifier",
        profile=None,
        model_purpose=purpose,
        max_tokens=max_tokens,
    )
    return LlmSecurityRouteClassifier(
        runner=runner,
        spec=spec,
        accept_threshold=config.llm_accept_threshold,
        default_top_k=config.default_top_k,
        min_score=config.min_score,
        max_tokens=max_tokens,
    )
