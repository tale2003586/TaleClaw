import os
from unittest.mock import patch

from applications.bootstrap import _build_shared_memory_embeddings
from memory.vector_index import NullMemoryVectorIndex
from memory.vector_runtime import (
    build_history_vector_index_from_env,
    build_semantic_memory_index_from_env,
)


def test_memory_indexes_accept_one_explicit_shared_embedding_provider() -> None:
    provider = object()
    history_index = object()
    semantic_index = object()
    with patch.dict(os.environ, {
        "HISTORY_VECTOR_ENABLED": "1",
        "SEMANTIC_MEMORY_INDEX_ENABLED": "1",
    }, clear=False), patch(
        "memory.vector_runtime.build_embedding_provider_from_env",
        side_effect=AssertionError("injected provider should be used"),
    ), patch(
        "memory.vector_runtime.QdrantMemoryVectorIndex",
        return_value=history_index,
    ) as history_factory, patch(
        "memory.vector_runtime.QdrantSemanticMemoryIndex",
        return_value=semantic_index,
    ) as semantic_factory:
        assert build_history_vector_index_from_env(embeddings=provider) is history_index
        assert build_semantic_memory_index_from_env(embeddings=provider) is semantic_index

    assert history_factory.call_args.kwargs["embeddings"] is provider
    assert semantic_factory.call_args.kwargs["embeddings"] is provider
    assert history_factory.call_args.kwargs["collection"] != semantic_factory.call_args.kwargs["collection"]


def test_history_failure_does_not_disable_semantic_index_with_shared_provider() -> None:
    provider = object()
    semantic_index = object()
    with patch.dict(os.environ, {
        "HISTORY_VECTOR_ENABLED": "1",
        "SEMANTIC_MEMORY_INDEX_ENABLED": "1",
    }, clear=False), patch(
        "memory.vector_runtime.QdrantMemoryVectorIndex",
        side_effect=RuntimeError("history qdrant unavailable"),
    ), patch(
        "memory.vector_runtime.QdrantSemanticMemoryIndex",
        return_value=semantic_index,
    ):
        history = build_history_vector_index_from_env(embeddings=provider)
        semantic = build_semantic_memory_index_from_env(embeddings=provider)

    assert isinstance(history, NullMemoryVectorIndex)
    assert semantic is semantic_index


def test_shared_memory_provider_is_optional_and_retries_fall_back_to_index_builders() -> None:
    provider = object()
    with patch(
        "memory.embeddings.build_embedding_provider_from_env",
        return_value=provider,
    ):
        assert _build_shared_memory_embeddings(
            history_enabled=True,
            semantic_enabled=True,
        ) is provider

    with patch(
        "memory.embeddings.build_embedding_provider_from_env",
        side_effect=RuntimeError("embedding unavailable"),
    ):
        assert _build_shared_memory_embeddings(
            history_enabled=True,
            semantic_enabled=True,
        ) is None

    assert _build_shared_memory_embeddings(
        history_enabled=False,
        semantic_enabled=True,
    ) is None
