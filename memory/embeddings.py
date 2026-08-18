from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import math
import os
import threading
import time
from typing import Any, Protocol


@dataclass(frozen=True)
class SparseEmbedding:
    indices: list[int]
    values: list[float]


class EmbeddingProvider(Protocol):
    @property
    def vector_size(self) -> int:
        ...

    def embed(self, text: str) -> list[float]:
        ...

    def embed_dense(self, text: str) -> list[float]:
        ...

    def embed_sparse(self, text: str) -> SparseEmbedding:
        ...


@dataclass
class HashEmbeddingProvider:
    """Small deterministic fallback used for tests and offline development."""

    dimensions: int = 384
    sparse_dimensions: int = 1_048_576

    @property
    def vector_size(self) -> int:
        return max(8, int(self.dimensions))

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.vector_size
        tokens = _tokens(text)
        if not tokens:
            return vector
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.vector_size
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        return _normalize(vector)

    def embed_dense(self, text: str) -> list[float]:
        return self.embed(text)

    def embed_sparse(self, text: str) -> SparseEmbedding:
        return _hashed_sparse(text, dimensions=max(1, int(self.sparse_dimensions)))


class FastEmbedProvider:
    def __init__(self, model_name: str, *, dimensions: int | None = None) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise RuntimeError(
                "fastembed is required when EMBEDDING_PROVIDER=fastembed. "
                "Install requirements.txt or switch EMBEDDING_PROVIDER=hash."
            ) from exc

        self.model_name = model_name
        self._lock = threading.RLock()
        self._model = TextEmbedding(model_name=model_name)
        self._dimensions = int(dimensions or 0)

    @property
    def vector_size(self) -> int:
        if self._dimensions > 0:
            return self._dimensions
        with self._lock:
            if self._dimensions <= 0:
                sample = self.embed("dimension probe")
                self._dimensions = len(sample)
            return self._dimensions

    def embed(self, text: str) -> list[float]:
        with self._lock:
            vectors = list(self._model.embed([text or ""]))
        if not vectors:
            return []
        vector = vectors[0]
        if hasattr(vector, "tolist"):
            return [float(value) for value in vector.tolist()]
        return [float(value) for value in vector]

    def embed_dense(self, text: str) -> list[float]:
        return self.embed(text)

    def embed_sparse(self, text: str) -> SparseEmbedding:
        return _hashed_sparse(text)


class CachedEmbeddingProvider:
    """Small in-process TTL cache for dense/sparse embedding calls."""

    def __init__(self, provider: EmbeddingProvider, *, max_size: int = 2048, ttl_seconds: int = 3600) -> None:
        self.provider = provider
        self.max_size = max(1, int(max_size))
        self.ttl_seconds = max(0, int(ttl_seconds))
        self._dense_cache: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()
        self._sparse_cache: OrderedDict[str, tuple[float, SparseEmbedding]] = OrderedDict()
        self._lock = threading.RLock()

    @property
    def vector_size(self) -> int:
        return self.provider.vector_size

    def embed(self, text: str) -> list[float]:
        return self.embed_dense(text)

    def embed_dense(self, text: str) -> list[float]:
        key = str(text or "")
        with self._lock:
            cached = self._get_dense(key)
        if cached is not None:
            return list(cached)
        vector = list(self.provider.embed_dense(key))
        with self._lock:
            self._put_dense(key, vector)
        return list(vector)

    def embed_sparse(self, text: str) -> SparseEmbedding:
        key = str(text or "")
        with self._lock:
            cached = self._get_sparse(key)
        if cached is not None:
            return SparseEmbedding(indices=list(cached.indices), values=list(cached.values))
        sparse = self.provider.embed_sparse(key)
        sparse = SparseEmbedding(indices=list(sparse.indices), values=list(sparse.values))
        with self._lock:
            self._put_sparse(key, sparse)
        return SparseEmbedding(indices=list(sparse.indices), values=list(sparse.values))

    def _get_dense(self, key: str) -> list[float] | None:
        item = self._get(self._dense_cache, key)
        return list(item) if item is not None else None

    def _put_dense(self, key: str, vector: list[float]) -> None:
        self._put(self._dense_cache, key, list(vector))

    def _get_sparse(self, key: str) -> SparseEmbedding | None:
        item = self._get(self._sparse_cache, key)
        if item is None:
            return None
        return SparseEmbedding(indices=list(item.indices), values=list(item.values))

    def _put_sparse(self, key: str, sparse: SparseEmbedding) -> None:
        self._put(
            self._sparse_cache,
            key,
            SparseEmbedding(indices=list(sparse.indices), values=list(sparse.values)),
        )

    def _get(self, cache: OrderedDict[str, tuple[float, Any]], key: str):
        if self.ttl_seconds <= 0:
            return None
        item = cache.get(key)
        if item is None:
            return None
        ts, value = item
        if time.monotonic() - ts > self.ttl_seconds:
            cache.pop(key, None)
            return None
        cache.move_to_end(key)
        return value

    def _put(self, cache: OrderedDict[str, tuple[float, Any]], key: str, value) -> None:
        if self.ttl_seconds <= 0:
            return
        cache[key] = (time.monotonic(), value)
        cache.move_to_end(key)
        while len(cache) > self.max_size:
            cache.popitem(last=False)


class BgeM3EmbeddingProvider:
    """Dense BGE-M3 embeddings through FlagEmbedding.

    BAAI/bge-m3 is not supported by fastembed.TextEmbedding in the currently
    pinned fastembed version, so it uses FlagEmbedding directly.
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        *,
        dimensions: int = 1024,
        use_fp16: bool = True,
        max_length: int = 8192,
        devices: str | list[str] | None = None,
    ) -> None:
        os.environ.setdefault("USE_TF", "0")
        os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
        os.environ.setdefault("USE_FLAX", "0")
        os.environ.setdefault("TRANSFORMERS_NO_FLAX", "1")
        os.environ.setdefault("PANDAS_USE_NUMEXPR", "0")
        os.environ.setdefault("PANDAS_USE_BOTTLENECK", "0")
        devices = _normalize_devices(devices)
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as exc:
            raise RuntimeError(
                "FlagEmbedding is required when EMBEDDING_PROVIDER=bge_m3. "
                "Install requirements.txt or switch EMBEDDING_PROVIDER=fastembed/hash."
            ) from exc

        self.model_name = model_name
        self._dimensions = int(dimensions or 1024)
        self._max_length = max(1, int(max_length or 8192))
        self.devices = devices
        self._lock = threading.RLock()
        try:
            self._model = BGEM3FlagModel(
                model_name,
                use_fp16=bool(use_fp16),
                devices=devices,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load BGE-M3 embedding model '{model_name}'. "
                "Make sure the model is downloaded, HF_ENDPOINT can reach "
                "Hugging Face, and EMBEDDING_DEVICE/SECURITY_RAG_EMBEDDING_DEVICE "
                "points to an available device."
            ) from exc

    @property
    def vector_size(self) -> int:
        return self._dimensions

    def embed(self, text: str) -> list[float]:
        normalized = str(text or "").strip()
        if not normalized:
            return [0.0] * self.vector_size
        try:
            with self._lock:
                output = self._model.encode(
                    [normalized],
                    batch_size=1,
                    max_length=self._max_length,
                    return_dense=True,
                    return_sparse=False,
                    return_colbert_vecs=False,
                )
        except IndexError:
            return HashEmbeddingProvider(dimensions=self.vector_size).embed(normalized)
        dense_vectors = output.get("dense_vecs") if isinstance(output, dict) else output
        if dense_vectors is None:
            return []
        vector = dense_vectors[0] if len(dense_vectors) else []
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        return [float(value) for value in vector]

    def embed_dense(self, text: str) -> list[float]:
        return self.embed(text)

    def embed_sparse(self, text: str) -> SparseEmbedding:
        normalized = str(text or "").strip()
        if not normalized:
            return _hashed_sparse(normalized)
        try:
            with self._lock:
                output = self._model.encode(
                    [normalized],
                    batch_size=1,
                    max_length=self._max_length,
                    return_dense=False,
                    return_sparse=True,
                    return_colbert_vecs=False,
                )
        except IndexError:
            return _hashed_sparse(normalized)
        weights = _first_sparse_weights(output)
        if not weights:
            return _hashed_sparse(text)
        items = sorted(_numeric_sparse_items(weights))
        return SparseEmbedding(
            indices=[index for index, _value in items],
            values=[value for _index, value in items],
        )


def build_embedding_provider_from_env() -> EmbeddingProvider:
    provider = os.getenv("EMBEDDING_PROVIDER", "hash").strip().lower()
    dimensions = _env_int("QDRANT_VECTOR_SIZE", 384)
    if provider == "fastembed":
        model = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5").strip()
        return FastEmbedProvider(model)
    if provider in {"bge_m3", "bge-m3", "flagembedding"}:
        model = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3").strip() or "BAAI/bge-m3"
        return BgeM3EmbeddingProvider(
            model,
            dimensions=dimensions,
            use_fp16=_env_bool("EMBEDDING_USE_FP16", True),
            max_length=_env_int("EMBEDDING_MAX_LENGTH", 8192),
            devices=_env_text("EMBEDDING_DEVICE", "") or None,
        )
    return HashEmbeddingProvider(dimensions=dimensions)


def _tokens(text: str) -> list[str]:
    return [
        token.strip().lower()
        for token in str(text or "").replace("\n", " ").split(" ")
        if token.strip()
    ]


def _hashed_sparse(text: str, *, dimensions: int = 1_048_576) -> SparseEmbedding:
    weights: dict[int, float] = {}
    for token in _tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        weights[index] = weights.get(index, 0.0) + 1.0
    if not weights:
        return SparseEmbedding(indices=[], values=[])
    norm = math.sqrt(sum(value * value for value in weights.values()))
    if norm <= 0:
        norm = 1.0
    items = sorted(weights.items())
    return SparseEmbedding(
        indices=[index for index, _value in items],
        values=[value / norm for _index, value in items],
    )


def _first_sparse_weights(output: Any) -> dict[Any, Any]:
    if not isinstance(output, dict):
        return {}
    candidates = (
        output.get("lexical_weights")
        or output.get("sparse_vecs")
        or output.get("sparse")
    )
    if isinstance(candidates, list) and candidates:
        first = candidates[0]
    else:
        first = candidates
    if isinstance(first, dict):
        return first
    return {}


def _numeric_sparse_items(weights: dict[Any, Any]) -> list[tuple[int, float]]:
    items: list[tuple[int, float]] = []
    for index, value in weights.items():
        try:
            numeric_index = int(index)
            numeric_value = float(value)
        except (TypeError, ValueError):
            continue
        if numeric_value != 0.0:
            items.append((numeric_index, numeric_value))
    return items


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return vector
    return [value / norm for value in vector]


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return int(default)
    try:
        return int(value)
    except ValueError:
        return int(default)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_text(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


def _normalize_devices(devices: str | list[str] | None) -> str | list[str] | None:
    if devices is None:
        return None
    if isinstance(devices, str):
        raw = devices.strip()
        if not raw:
            return None
        if "," in raw:
            return [item.strip() for item in raw.split(",") if item.strip()]
        return raw
    return devices
