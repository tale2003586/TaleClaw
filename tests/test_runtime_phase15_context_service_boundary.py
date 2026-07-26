from __future__ import annotations

import inspect

from runtime.context import ContextBuilder


LEGACY_RETRIEVAL_PARAMETERS = {
    "history_vector_index",
    "history_scope_resolver",
    "memory_vector_index",
    "memory_scope_resolver",
    "retrieval_top_k",
    "retrieval_min_score",
    "security_retrieval_router",
    "security_route_classifier",
    "security_knowledge_index",
    "security_auto_context_enabled",
}


def test_context_builder_accepts_only_the_retrieval_service_boundary():
    parameters = set(inspect.signature(ContextBuilder).parameters)

    assert "retrieval_service" in parameters
    assert parameters.isdisjoint(LEGACY_RETRIEVAL_PARAMETERS)


def test_context_builder_source_has_no_concrete_retrieval_configuration_names():
    source = inspect.getsource(ContextBuilder)

    assert all(name not in source for name in LEGACY_RETRIEVAL_PARAMETERS)
    assert "ContextRetrievalService" not in source
