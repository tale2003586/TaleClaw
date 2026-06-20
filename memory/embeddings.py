from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
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
        self._model = TextEmbedding(model_name=model_name)
        self._dimensions = int(dimensions or 0)

    @property
    def vector_size(self) -> int:
        if self._dimensions > 0:
            return self._dimensions
        sample = self.embed("dimension probe")
        self._dimensions = len(sample)
        return self._dimensions

    def embed(self, text: str) -> list[float]:
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
        output = self._model.encode(
            [text or ""],
            max_length=self._max_length,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
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
        output = self._model.encode(
            [text or ""],
            max_length=self._max_length,
            return_dense=False,
            return_sparse=True,
            return_colbert_vecs=False,
        )
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
