from __future__ import annotations

import os

from memory.embeddings import build_embedding_provider_from_env
from memory.qdrant_index import QdrantMemoryVectorIndex
from memory.vector_index import MemoryVectorIndex, NullMemoryVectorIndex
from user_scope import user_id_for_session


def build_history_vector_index_from_env() -> MemoryVectorIndex:
    if not _env_bool_any(
        ["HISTORY_VECTOR_ENABLED", "MEMORY_VECTOR_ENABLED"],
        default=True,
    ):
        return NullMemoryVectorIndex()

    backend = _env_first(
        ["HISTORY_VECTOR_BACKEND", "MEMORY_VECTOR_BACKEND"],
        "qdrant",
    ).strip().lower()
    if backend != "qdrant":
        raise RuntimeError(f"Unsupported HISTORY_VECTOR_BACKEND: {backend}")

    try:
        embeddings = build_embedding_provider_from_env()
        return QdrantMemoryVectorIndex(
            url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333").strip(),
            api_key=os.getenv("QDRANT_API_KEY", "").strip() or None,
            collection=os.getenv("QDRANT_COLLECTION", "taleclaw_history").strip(),
            distance=os.getenv("QDRANT_DISTANCE", "Cosine").strip(),
            embeddings=embeddings,
        )
    except Exception:
        if _env_bool("HISTORY_VECTOR_STRICT", default=False):
            raise
        return NullMemoryVectorIndex()


def history_vector_scope_for_session(session) -> str:
    metadata = getattr(session, "metadata", {}) or {}
    if metadata.get("kind") == "task_session":
        task_id = metadata.get("task_id") or getattr(session, "id", "unknown")
        return f"task:{task_id}"
    return f"user:{user_id_for_session(session)}"


def build_memory_vector_index_from_env() -> MemoryVectorIndex:
    return build_history_vector_index_from_env()


def memory_vector_scope_for_session(session) -> str:
    return history_vector_scope_for_session(session)


def _env_first(names: list[str], default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value
    return default


def _env_bool_any(names: list[str], *, default: bool) -> bool:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
