from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import time
from dataclasses import dataclass, asdict, replace
from pathlib import Path
from typing import Any, Iterable

from knowledge.caching import CachedSecurityIndex
from knowledge.chunking import ChunkingRouter
from knowledge.chunking.base import KnowledgeChunk
from memory.embeddings import (
    BgeM3EmbeddingProvider,
    EmbeddingProvider,
    FastEmbedProvider,
    HashEmbeddingProvider,
    SparseEmbedding,
)


DEFAULT_SOURCE_ROOT = Path("/home/tale/kaggle/code-security-kb")
DEFAULT_COLLECTION = "code_security_kb"
TEXT_SUFFIXES = {
    ".md",
    ".markdown",
    ".txt",
    ".yaml",
    ".yml",
    ".json",
    ".py",
    ".rst",
}
SKIP_DIRS = {
    ".git",
    ".github",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
}


@dataclass(frozen=True)
class KnowledgeHit:
    id: str
    text: str
    score: float
    source_path: str
    source_relpath: str
    title: str
    chunk_index: int
    metadata: dict


class SecurityKnowledgeIndex:
    def __init__(
        self,
        *,
        url: str,
        collection: str,
        embeddings: EmbeddingProvider,
        api_key: str | None = None,
        distance: str = "Cosine",
        hybrid_enabled: bool = False,
        sparse_embeddings: EmbeddingProvider | None = None,
        reranker: Any = None,
        reranker_candidates: int = 30,
    ) -> None:
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import (
                Distance,
                FieldCondition,
                Filter,
                Fusion,
                FusionQuery,
                MatchAny,
                MatchValue,
                Prefetch,
                SparseVector,
                SparseVectorParams,
                VectorParams,
            )
        except ImportError as exc:
            raise RuntimeError(
                "qdrant-client is required for the security RAG knowledge base. "
                "Install requirements.txt first."
            ) from exc

        self.collection = collection
        self.embeddings = embeddings
        self.sparse_embeddings = sparse_embeddings or embeddings
        self.hybrid_enabled = bool(hybrid_enabled)
        self.reranker = reranker
        self.reranker_candidates = max(1, int(reranker_candidates or 30))
        self._client = QdrantClient(
            url=url,
            api_key=api_key or None,
            check_compatibility=False,
            trust_env=False,
        )
        self._Distance = Distance
        self._VectorParams = VectorParams
        self._SparseVector = SparseVector
        self._SparseVectorParams = SparseVectorParams
        self._Prefetch = Prefetch
        self._Fusion = Fusion
        self._FusionQuery = FusionQuery
        self._Filter = Filter
        self._FieldCondition = FieldCondition
        self._MatchValue = MatchValue
        self._MatchAny = MatchAny
        self.ensure_collection(distance=distance)

    def ensure_collection(self, *, distance: str = "Cosine") -> None:
        if self.collection_exists():
            return
        self._client.create_collection(**self._collection_create_kwargs(distance=distance))

    def recreate_collection(self, *, distance: str = "Cosine") -> None:
        if self.collection_exists():
            self._client.delete_collection(collection_name=self.collection)
        self._client.create_collection(**self._collection_create_kwargs(distance=distance))

    def collection_exists(self) -> bool:
        if hasattr(self._client, "collection_exists"):
            return bool(self._client.collection_exists(self.collection))
        try:
            self._client.get_collection(self.collection)
            return True
        except Exception:
            return False

    def upsert_chunks(self, chunks: list[KnowledgeChunk], *, batch_size: int = 64) -> int:
        from qdrant_client.models import PointStruct

        indexed = 0
        for batch in _batches(chunks, max(1, int(batch_size))):
            points = []
            for chunk in batch:
                payload = asdict(chunk)
                payload["text"] = chunk.text
                points.append(
                    PointStruct(
                        id=_point_id(chunk.id),
                        vector=self._point_vector(chunk.text),
                        payload=payload,
                    )
                )
            if points:
                self._client.upsert(collection_name=self.collection, points=points)
                indexed += len(points)
        return indexed

    def delete_file_chunks(self, source_path: str | Path) -> None:
        query_filter = self._Filter(must=[
            self._FieldCondition(
                key="source_path",
                match=self._MatchValue(value=str(source_path)),
            )
        ])
        self._client.delete(
            collection_name=self.collection,
            points_selector=query_filter,
            wait=True,
        )

    def search(
        self,
        query: str,
        *,
        top_k: int = 6,
        min_score: float = 0.0,
        use_reranker: bool | None = None,
        source_type: str | None = None,
        severity: str | list[str] | None = None,
        language: str | None = None,
        trace_callback=None,
        use_cache: bool = True,
    ) -> list[KnowledgeHit]:
        top_k = max(1, int(top_k))
        candidate_limit = max(top_k, self.reranker_candidates)
        query_filter = self._payload_filter(
            source_type=source_type,
            severity=severity,
            language=language,
        )
        timings: dict[str, float] = {}
        started = time.perf_counter()
        try:
            if self.hybrid_enabled:
                points = self._query_points_hybrid(
                    query=query,
                    limit=candidate_limit,
                    query_filter=query_filter,
                )
                timings["hybrid_search"] = _elapsed_ms(started)
            else:
                vector = self.embeddings.embed_dense(query)
                points = self._query_points_dense(
                    vector=vector,
                    limit=candidate_limit,
                    query_filter=query_filter,
                )
                timings["dense_search"] = _elapsed_ms(started)
        except Exception:
            if not self.hybrid_enabled:
                raise
            fallback_started = time.perf_counter()
            vector = self.embeddings.embed_dense(query)
            points = self._query_points_dense(
                vector=vector,
                limit=candidate_limit,
                query_filter=query_filter,
            )
            timings["hybrid_fallback_dense"] = _elapsed_ms(fallback_started)

        hits = [
            hit
            for hit in (self._point_to_hit(point) for point in points)
            if hit.score >= float(min_score)
        ]
        should_rerank = (
            self.reranker is not None
            if use_reranker is None
            else bool(use_reranker) and self.reranker is not None
        )
        if should_rerank and hits:
            rerank_started = time.perf_counter()
            hits = self._rerank_hits(query, hits)
            timings["rerank"] = _elapsed_ms(rerank_started)
        final_hits = hits[:top_k]
        timings["total"] = _elapsed_ms(started)
        if trace_callback is not None:
            trace_callback({
                "query": query,
                "hybrid_enabled": self.hybrid_enabled,
                "reranker_enabled": should_rerank,
                "candidate_count": len(hits),
                "final_count": len(final_hits),
                "latency_ms": timings,
            })
        return final_hits

    def _query_points_dense(self, *, vector: list[float], limit: int, query_filter=None):
        if hasattr(self._client, "query_points"):
            response = self._client.query_points(
                collection_name=self.collection,
                query=vector,
                using="dense" if self.hybrid_enabled else None,
                with_payload=True,
                limit=limit,
                query_filter=query_filter,
            )
            return list(getattr(response, "points", []) or [])
        return list(
            self._client.search(
                collection_name=self.collection,
                query_vector=vector,
                with_payload=True,
                limit=limit,
                query_filter=query_filter,
            )
        )

    def _query_points_hybrid(self, *, query: str, limit: int, query_filter=None):
        dense = self.embeddings.embed_dense(query)
        sparse = self._to_qdrant_sparse(self._sparse_embeddings().embed_sparse(query))
        response = self._client.query_points(
            collection_name=self.collection,
            prefetch=[
                self._Prefetch(
                    query=dense,
                    using="dense",
                    limit=limit,
                    filter=query_filter,
                ),
                self._Prefetch(
                    query=sparse,
                    using="sparse",
                    limit=limit,
                    filter=query_filter,
                ),
            ],
            query=self._FusionQuery(fusion=self._Fusion.RRF),
            with_payload=True,
            limit=limit,
            query_filter=query_filter,
        )
        return list(getattr(response, "points", []) or [])

    def _point_to_hit(self, point) -> KnowledgeHit:
        payload = getattr(point, "payload", {}) or {}
        return KnowledgeHit(
            id=str(payload.get("id") or getattr(point, "id", "")),
            text=str(payload.get("text") or ""),
            score=float(getattr(point, "score", 0.0) or 0.0),
            source_path=str(payload.get("source_path") or ""),
            source_relpath=str(payload.get("source_relpath") or ""),
            title=str(payload.get("title") or ""),
            chunk_index=int(payload.get("chunk_index") or 0),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )

    def _rerank_hits(self, query: str, hits: list[KnowledgeHit]) -> list[KnowledgeHit]:
        scored = self.reranker.rerank(query, hits)
        return [
            replace(hit, score=float(score))
            for score, hit in scored
        ]

    def _collection_create_kwargs(self, *, distance: str) -> dict:
        kwargs = {
            "collection_name": self.collection,
            "vectors_config": self._VectorParams(
                size=self.embeddings.vector_size,
                distance=self._distance(distance),
            ),
        }
        if self.hybrid_enabled:
            kwargs["vectors_config"] = {
                "dense": self._VectorParams(
                    size=self.embeddings.vector_size,
                    distance=self._distance(distance),
                ),
            }
            kwargs["sparse_vectors_config"] = {
                "sparse": self._SparseVectorParams(),
            }
        return kwargs

    def _point_vector(self, text: str):
        dense = self.embeddings.embed_dense(text)
        if not self.hybrid_enabled:
            return dense
        return {
            "dense": dense,
            "sparse": self._to_qdrant_sparse(self._sparse_embeddings().embed_sparse(text)),
        }

    def _sparse_embeddings(self) -> EmbeddingProvider:
        return getattr(self, "sparse_embeddings", None) or self.embeddings

    def _to_qdrant_sparse(self, sparse: Any):
        if isinstance(sparse, self._SparseVector):
            return sparse
        if isinstance(sparse, SparseEmbedding):
            return self._SparseVector(indices=sparse.indices, values=sparse.values)
        if isinstance(sparse, dict):
            return self._SparseVector(
                indices=[int(index) for index in sparse.get("indices", [])],
                values=[float(value) for value in sparse.get("values", [])],
            )
        return self._SparseVector(indices=[], values=[])

    def _payload_filter(
        self,
        *,
        source_type: str | None = None,
        severity: str | list[str] | None = None,
        language: str | None = None,
    ):
        conditions = []
        if source_type:
            conditions.append(self._FieldCondition(
                key="source_type",
                match=self._MatchValue(value=source_type),
            ))
        if severity:
            values = severity if isinstance(severity, list) else [severity]
            conditions.append(self._FieldCondition(
                key="metadata.severity",
                match=self._MatchAny(any=[str(item) for item in values]),
            ))
        if language:
            conditions.append(self._FieldCondition(
                key="metadata.language",
                match=self._MatchValue(value=language),
            ))
        return self._Filter(must=conditions) if conditions else None

    def _distance(self, value: str):
        normalized = str(value or "Cosine").strip().upper()
        if normalized == "DOT":
            return self._Distance.DOT
        if normalized in {"EUCLID", "EUCLIDEAN"}:
            return self._Distance.EUCLID
        if normalized == "MANHATTAN":
            return self._Distance.MANHATTAN
        return self._Distance.COSINE


def build_security_index_from_env(
    *,
    collection: str | None = None,
    embeddings: EmbeddingProvider | None = None,
    sparse_embeddings: EmbeddingProvider | None = None,
):
    embeddings = embeddings or build_security_embedding_provider_from_env()
    index = SecurityKnowledgeIndex(
        url=os.getenv("SECURITY_RAG_QDRANT_URL", os.getenv("QDRANT_URL", "http://127.0.0.1:6333")).strip(),
        api_key=os.getenv("SECURITY_RAG_QDRANT_API_KEY", os.getenv("QDRANT_API_KEY", "")).strip(),
        collection=collection
        or os.getenv("SECURITY_RAG_COLLECTION", DEFAULT_COLLECTION).strip()
        or DEFAULT_COLLECTION,
        embeddings=embeddings,
        sparse_embeddings=(
            sparse_embeddings
            if sparse_embeddings is not None
            else build_security_sparse_embedding_provider_from_env()
        ),
        distance=os.getenv("SECURITY_RAG_DISTANCE", os.getenv("QDRANT_DISTANCE", "Cosine")).strip(),
        hybrid_enabled=_env_bool("SECURITY_RAG_HYBRID_ENABLED", False),
        reranker=_build_reranker_from_env(),
        reranker_candidates=_env_int("SECURITY_RAG_RERANKER_CANDIDATES", 30),
    )
    if _env_bool("SECURITY_RAG_CACHE_ENABLED", True):
        return CachedSecurityIndex(
            index,
            max_size=_env_int("SECURITY_RAG_CACHE_MAX_SIZE", 512),
            ttl_seconds=_env_int("SECURITY_RAG_CACHE_TTL_SECONDS", 3600),
        )
    return index


def build_security_embedding_provider_from_env() -> EmbeddingProvider:
    provider = os.getenv("SECURITY_RAG_EMBEDDING_PROVIDER", "fastembed").strip().lower()
    if provider == "fastembed":
        model = os.getenv("SECURITY_RAG_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5").strip()
        return FastEmbedProvider(model)
    dimensions = _env_int("SECURITY_RAG_VECTOR_SIZE", _env_int("QDRANT_VECTOR_SIZE", 512))
    if provider in {"bge_m3", "bge-m3", "flagembedding"}:
        model = os.getenv("SECURITY_RAG_EMBEDDING_MODEL", "BAAI/bge-m3").strip() or "BAAI/bge-m3"
        return BgeM3EmbeddingProvider(
            model,
            dimensions=dimensions,
            use_fp16=_env_bool("SECURITY_RAG_EMBEDDING_USE_FP16", True),
            max_length=_env_int("SECURITY_RAG_EMBEDDING_MAX_LENGTH", 8192),
            devices=(
                _env_text(
                    "SECURITY_RAG_EMBEDDING_DEVICE",
                    _env_text("EMBEDDING_DEVICE", ""),
                )
                or None
            ),
        )
    return HashEmbeddingProvider(
        dimensions=dimensions,
        sparse_dimensions=_env_int("SECURITY_RAG_SPARSE_HASH_SIZE", 1_048_576),
    )


def build_security_sparse_embedding_provider_from_env() -> EmbeddingProvider | None:
    provider = os.getenv("SECURITY_RAG_SPARSE_EMBEDDING_PROVIDER", "").strip().lower()
    if not provider or provider in {"same", "dense", "default"}:
        return None
    dimensions = _env_int("SECURITY_RAG_VECTOR_SIZE", _env_int("QDRANT_VECTOR_SIZE", 512))
    if provider == "hash":
        return HashEmbeddingProvider(
            dimensions=dimensions,
            sparse_dimensions=_env_int("SECURITY_RAG_SPARSE_HASH_SIZE", 1_048_576),
        )
    if provider == "fastembed":
        model = (
            os.getenv("SECURITY_RAG_SPARSE_EMBEDDING_MODEL", "").strip()
            or os.getenv("SECURITY_RAG_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5").strip()
        )
        return FastEmbedProvider(model)
    if provider in {"bge_m3", "bge-m3", "flagembedding"}:
        model = (
            os.getenv("SECURITY_RAG_SPARSE_EMBEDDING_MODEL", "").strip()
            or os.getenv("SECURITY_RAG_EMBEDDING_MODEL", "BAAI/bge-m3").strip()
            or "BAAI/bge-m3"
        )
        return BgeM3EmbeddingProvider(
            model,
            dimensions=dimensions,
            use_fp16=_env_bool("SECURITY_RAG_EMBEDDING_USE_FP16", True),
            max_length=_env_int("SECURITY_RAG_EMBEDDING_MAX_LENGTH", 8192),
            devices=(
                _env_text(
                    "SECURITY_RAG_EMBEDDING_DEVICE",
                    _env_text("EMBEDDING_DEVICE", ""),
                )
                or None
            ),
        )
    return None


def _build_reranker_from_env():
    if not _env_bool("SECURITY_RAG_RERANKER_ENABLED", False):
        return None
    from knowledge.reranker import RerankerProvider

    return RerankerProvider(
        model_name=os.getenv(
            "SECURITY_RAG_RERANKER_MODEL",
            "BAAI/bge-reranker-v2-m3",
        ).strip() or "BAAI/bge-reranker-v2-m3",
        use_fp16=_env_bool("SECURITY_RAG_RERANKER_USE_FP16", True),
    )


def iter_source_files(
    root: Path,
    *,
    suffixes: set[str] | None = None,
    max_file_bytes: int = 1_000_000,
    limit: int | None = None,
) -> Iterable[Path]:
    suffixes = suffixes or TEXT_SUFFIXES
    count = 0
    for path in root.rglob("*"):
        if limit is not None and count >= limit:
            break
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in suffixes:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= 0 or size > max_file_bytes:
            continue
        count += 1
        yield path


def chunks_from_file(
    path: Path,
    *,
    root: Path,
    chunk_chars: int = 1800,
    overlap_chars: int = 220,
) -> list[KnowledgeChunk]:
    return ChunkingRouter().chunks_from_file(
        path,
        root=root,
        chunk_chars=chunk_chars,
        overlap_chars=overlap_chars,
    )


def _legacy_chunks_from_file(
    path: Path,
    *,
    root: Path,
    chunk_chars: int = 1800,
    overlap_chars: int = 220,
) -> list[KnowledgeChunk]:
    text = read_text_file(path)
    if not text.strip():
        return []
    relpath = str(path.relative_to(root)) if path.is_relative_to(root) else path.name
    suffix = path.suffix.lower()
    if suffix == ".json":
        advisory_chunks = chunks_from_json_advisory(
            path,
            root=root,
            text=text,
            chunk_chars=chunk_chars,
        )
        if advisory_chunks:
            return advisory_chunks
    if suffix in {".yaml", ".yml"}:
        rule_chunks = chunks_from_semgrep_yaml(
            path,
            root=root,
            text=text,
            chunk_chars=chunk_chars,
        )
        if rule_chunks:
            return rule_chunks
    if suffix in {".md", ".markdown", ".rst"}:
        return chunks_from_markdown_doc(
            path,
            root=root,
            text=text,
            chunk_chars=chunk_chars,
            overlap_chars=overlap_chars,
        )
    return chunks_from_plain_text(
        path,
        root=root,
        text=text,
        chunk_chars=chunk_chars,
        overlap_chars=overlap_chars,
        relpath=relpath,
    )


def chunks_from_json_advisory(
    path: Path,
    *,
    root: Path,
    text: str,
    chunk_chars: int = 1800,
) -> list[KnowledgeChunk]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict) or not _looks_like_advisory(data):
        return []
    relpath = str(path.relative_to(root)) if path.is_relative_to(root) else path.name
    advisory_id = str(data.get("id") or path.stem)
    aliases = [str(item) for item in _as_list(data.get("aliases")) if str(item).strip()]
    severity = _format_severity(data.get("severity"))
    packages = _affected_packages(data)
    cwes = _cwe_list(data)
    header = _format_advisory_header(
        advisory_id=advisory_id,
        aliases=aliases,
        severity=severity,
        published=str(data.get("published") or ""),
        modified=str(data.get("modified") or ""),
        packages=packages,
        cwes=cwes,
    )
    base_metadata = {
        "corpus_type": "advisory",
        "advisory_id": advisory_id,
        "aliases": aliases,
        "severity": severity,
        "packages": packages,
        "cwes": cwes,
        "filename": path.name,
        "parent": path.parent.name,
    }
    pieces: list[tuple[str, str, int, int, dict[str, Any]]] = []
    summary = str(data.get("summary") or "").strip()
    if summary:
        pieces.append(("summary", f"{header}\n\nSummary:\n{summary}", _field_start(text, "summary"), _field_end(text, "summary"), {}))
    details = str(data.get("details") or data.get("description") or "").strip()
    if details:
        detail_start = _field_start(text, "details")
        for index, (chunk_text, start, end) in enumerate(
            split_semantic_text(details, chunk_chars=chunk_chars, overlap_chars=0)
        ):
            pieces.append(
                (
                    "details",
                    f"{header}\n\nDetails part {index + 1}:\n{chunk_text}",
                    max(0, detail_start + start),
                    max(0, detail_start + end),
                    {"part": index + 1},
                )
            )
    affected = _format_affected(data.get("affected"))
    if affected:
        pieces.append(("affected", f"{header}\n\nAffected packages and ranges:\n{affected}", _field_start(text, "affected"), _field_end(text, "affected"), {}))
    references = _format_references(data.get("references"))
    if references:
        pieces.append(("references", f"{header}\n\nReferences:\n{references}", _field_start(text, "references"), _field_end(text, "references"), {}))
    if not pieces:
        compact = json.dumps(data, ensure_ascii=False, indent=2)
        for index, (chunk_text, start, end) in enumerate(split_semantic_text(compact, chunk_chars=chunk_chars, overlap_chars=0)):
            pieces.append(("raw", f"{header}\n\nRaw advisory part {index + 1}:\n{chunk_text}", start, end, {"part": index + 1}))
    chunks: list[KnowledgeChunk] = []
    title = _advisory_title(data, advisory_id)
    for field_name, body, start, end, extra_metadata in pieces:
        metadata = {**base_metadata, "field": field_name, **extra_metadata}
        chunks.append(
            _make_chunk(
                path=path,
                relpath=relpath,
                title=title,
                body=body,
                chunk_index=len(chunks),
                char_start=max(0, start),
                char_end=max(0, end),
                source_type="json_advisory",
                metadata=metadata,
            )
        )
    return chunks


def chunks_from_semgrep_yaml(
    path: Path,
    *,
    root: Path,
    text: str,
    chunk_chars: int = 1800,
) -> list[KnowledgeChunk]:
    try:
        import yaml
    except ImportError:
        return []
    try:
        docs = list(yaml.safe_load_all(text))
    except Exception:
        return []
    rules: list[dict[str, Any]] = []
    for doc in docs:
        if isinstance(doc, dict) and isinstance(doc.get("rules"), list):
            rules.extend([rule for rule in doc["rules"] if isinstance(rule, dict)])
    if not rules:
        return []
    relpath = str(path.relative_to(root)) if path.is_relative_to(root) else path.name
    chunks: list[KnowledgeChunk] = []
    for rule_index, rule in enumerate(rules):
        rule_id = str(rule.get("id") or f"{path.stem}:{rule_index + 1}")
        metadata = _semgrep_metadata(rule)
        header = _format_semgrep_header(rule_id=rule_id, metadata=metadata)
        rendered_rule = _format_semgrep_rule(rule)
        if len(rendered_rule) <= chunk_chars:
            parts = [(rendered_rule, 0, len(rendered_rule), 1)]
        else:
            pattern_text = _format_semgrep_patterns(rule)
            prefix = _format_semgrep_rule(rule, include_patterns=False)
            parts = []
            if prefix.strip():
                parts.append((prefix, 0, len(prefix), 1))
            for part_index, (chunk_text, start, end) in enumerate(
                split_semantic_text(pattern_text, chunk_chars=chunk_chars, overlap_chars=0),
                start=2,
            ):
                parts.append((f"Patterns part {part_index - 1}:\n{chunk_text}", start, end, part_index))
        rule_start = _rule_start(text, rule_id)
        for body, start, end, part in parts:
            part_body = f"{header}\n\n{body}"
            chunks.append(
                _make_chunk(
                    path=path,
                    relpath=relpath,
                    title=rule_id,
                    body=part_body,
                    chunk_index=len(chunks),
                    char_start=max(0, rule_start + start),
                    char_end=max(0, rule_start + end),
                    source_type="semgrep_yaml",
                    metadata={
                        **metadata,
                        "corpus_type": "semgrep_rule",
                        "rule_id": rule_id,
                        "part": part,
                        "filename": path.name,
                        "parent": path.parent.name,
                    },
                )
            )
    return chunks


def chunks_from_markdown_doc(
    path: Path,
    *,
    root: Path,
    text: str,
    chunk_chars: int = 1800,
    overlap_chars: int = 220,
) -> list[KnowledgeChunk]:
    relpath = str(path.relative_to(root)) if path.is_relative_to(root) else path.name
    title = infer_title(path, text)
    chunks: list[KnowledgeChunk] = []
    for heading_path, section_text, section_start in split_markdown_sections(text):
        base_title = heading_path or title
        for item_text, start, end in split_semantic_text(
            section_text,
            chunk_chars=chunk_chars,
            overlap_chars=overlap_chars,
        ):
            chunks.append(
                _make_chunk(
                    path=path,
                    relpath=relpath,
                    title=base_title,
                    body=item_text,
                    chunk_index=len(chunks),
                    char_start=section_start + start,
                    char_end=section_start + end,
                    source_type="markdown",
                    metadata={
                        "corpus_type": "markdown_doc",
                        "filename": path.name,
                        "parent": path.parent.name,
                        "heading": heading_path,
                    },
                )
            )
    return chunks


def chunks_from_plain_text(
    path: Path,
    *,
    root: Path,
    text: str,
    chunk_chars: int,
    overlap_chars: int,
    relpath: str | None = None,
) -> list[KnowledgeChunk]:
    relpath = relpath or (str(path.relative_to(root)) if path.is_relative_to(root) else path.name)
    title = infer_title(path, text)
    chunks: list[KnowledgeChunk] = []
    for item_text, start, end in split_semantic_text(text, chunk_chars=chunk_chars, overlap_chars=overlap_chars):
        chunks.append(
            _make_chunk(
                path=path,
                relpath=relpath,
                title=title,
                body=item_text,
                chunk_index=len(chunks),
                char_start=start,
                char_end=end,
                source_type=path.suffix.lower().lstrip(".") or "text",
                metadata={
                    "corpus_type": "plain_text",
                    "filename": path.name,
                    "parent": path.parent.name,
                    "heading": "",
                },
            )
        )
    return chunks


def _make_chunk(
    *,
    path: Path,
    relpath: str,
    title: str,
    body: str,
    chunk_index: int,
    char_start: int,
    char_end: int,
    source_type: str,
    metadata: dict,
) -> KnowledgeChunk:
    chunk_id = chunk_stable_id(path, char_start, char_end, body)
    return KnowledgeChunk(
        id=chunk_id,
        text=render_chunk_text(
            title=title,
            relpath=relpath,
            body=body,
        ),
        source_path=str(path),
        source_relpath=relpath,
        title=title,
        chunk_index=chunk_index,
        char_start=char_start,
        char_end=char_end,
        source_type=source_type,
        metadata=metadata,
    )


def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def split_markdown_sections(text: str) -> list[tuple[str, str, int]]:
    matches = list(re.finditer(r"(?m)^(#{1,6})\s+(.+?)\s*$", text))
    if not matches:
        return [("", text, 0)]
    sections: list[tuple[str, str, int]] = []
    if matches[0].start() > 0:
        sections.append(("", text[: matches[0].start()], 0))
    heading_stack: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        level = len(match.group(1))
        heading = match.group(2).strip()
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_stack.append((level, heading))
        heading_path = " > ".join(item[1] for item in heading_stack)
        sections.append((heading_path, text[match.start() : end], match.start()))
    return [(title, body, start) for title, body, start in sections if body.strip()]


def split_text(text: str, *, chunk_chars: int, overlap_chars: int) -> list[tuple[str, int, int]]:
    return split_semantic_text(text, chunk_chars=chunk_chars, overlap_chars=overlap_chars)


def split_semantic_text(text: str, *, chunk_chars: int, overlap_chars: int) -> list[tuple[str, int, int]]:
    text = normalize_text(text)
    if len(text) <= chunk_chars:
        return [(text, 0, len(text))]
    chunks: list[tuple[str, int, int]] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_chars)
        if end < len(text):
            split_at = _semantic_split_point(text, start, end)
            if split_at > start + chunk_chars // 2:
                end = split_at + 1
            end = _avoid_open_code_fence(text, start, end) if end < len(text) else end
        chunk = text[start:end].strip()
        if chunk:
            chunks.append((chunk, start, end))
        if end >= len(text):
            break
        start = _semantic_overlap_start(text, end, overlap_chars)
    return chunks


def _semantic_split_point(text: str, start: int, end: int) -> int:
    candidates = [
        text.rfind("\n\n", start, end),
        text.rfind("\n- ", start, end),
        text.rfind("\n* ", start, end),
        text.rfind("\n|", start, end),
        text.rfind("\n", start, end),
        text.rfind(". ", start, end),
        text.rfind("。", start, end),
    ]
    return max(candidates)


def _semantic_overlap_start(text: str, end: int, overlap_chars: int) -> int:
    if overlap_chars <= 0:
        return end
    raw_start = max(0, end - overlap_chars)
    paragraph_start = text.find("\n\n", raw_start, end)
    if paragraph_start != -1 and paragraph_start + 2 < end:
        return paragraph_start + 2
    line_start = text.find("\n", raw_start, end)
    if line_start != -1 and line_start + 1 < end:
        return line_start + 1
    return raw_start


def _avoid_open_code_fence(text: str, start: int, end: int) -> int:
    window = text[start:end]
    if window.count("```") % 2 == 0:
        return end
    fence_start = window.rfind("```")
    if fence_start > len(window) // 2:
        return start + fence_start
    fence_end = text.find("```", end)
    if fence_end != -1 and fence_end + 3 - start <= int((end - start) * 1.25):
        return fence_end + 3
    return end


def normalize_text(text: str) -> str:
    return re.sub(r"\n{4,}", "\n\n\n", str(text or "").replace("\r\n", "\n").replace("\r", "\n")).strip()


def infer_title(path: Path, text: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    if match:
        return match.group(1).strip()
    return path.stem.replace("_", " ").replace("-", " ").strip() or path.name


def render_chunk_text(*, title: str, relpath: str, body: str) -> str:
    return f"TITLE: {title}\nSOURCE: {relpath}\n\n{body.strip()}"


def _looks_like_advisory(data: dict[str, Any]) -> bool:
    advisory_id = str(data.get("id") or "")
    aliases = _as_list(data.get("aliases"))
    return bool(
        advisory_id.startswith(("GHSA-", "CVE-"))
        or any(str(alias).startswith("CVE-") for alias in aliases)
        or {"summary", "affected", "references"}.issubset(data.keys())
    )


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _format_severity(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        rendered = []
        for item in value:
            if isinstance(item, dict):
                score = item.get("score")
                severity_type = item.get("type")
                if score and severity_type:
                    rendered.append(f"{severity_type}:{score}")
                elif score:
                    rendered.append(str(score))
            elif item:
                rendered.append(str(item))
        return ", ".join(rendered)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return ""


def _affected_packages(data: dict[str, Any]) -> list[str]:
    packages: list[str] = []
    for item in _as_list(data.get("affected")):
        if not isinstance(item, dict):
            continue
        package = item.get("package")
        if not isinstance(package, dict):
            continue
        ecosystem = str(package.get("ecosystem") or "").strip()
        name = str(package.get("name") or "").strip()
        if ecosystem and name:
            packages.append(f"{ecosystem}:{name}")
        elif name:
            packages.append(name)
    return _dedupe_strings(packages)


def _cwe_list(data: dict[str, Any]) -> list[str]:
    cwes: list[str] = []
    database_specific = data.get("database_specific")
    if isinstance(database_specific, dict):
        cwes.extend(str(item) for item in _as_list(database_specific.get("cwe_ids")))
        cwes.extend(str(item) for item in _as_list(database_specific.get("cwes")))
    for item in _as_list(data.get("cwes")):
        cwes.append(str(item))
    return _dedupe_strings([item for item in cwes if item and item != "None"])


def _format_advisory_header(
    *,
    advisory_id: str,
    aliases: list[str],
    severity: str,
    published: str,
    modified: str,
    packages: list[str],
    cwes: list[str],
) -> str:
    lines = [
        "CVE/GHSA Advisory",
        f"ID: {advisory_id}",
        f"Aliases: {', '.join(aliases) if aliases else 'None'}",
        f"Severity: {severity or 'UNKNOWN'}",
        f"Published: {published or 'UNKNOWN'}",
        f"Modified: {modified or 'UNKNOWN'}",
        f"Package: {', '.join(packages) if packages else 'UNKNOWN'}",
        f"CWEs: {', '.join(cwes) if cwes else 'UNKNOWN'}",
    ]
    return "\n".join(lines)


def _advisory_title(data: dict[str, Any], advisory_id: str) -> str:
    summary = str(data.get("summary") or "").strip()
    if summary:
        return f"{advisory_id}: {summary[:120]}"
    return advisory_id


def _format_affected(value: Any) -> str:
    affected = _as_list(value)
    if not affected:
        return ""
    return json.dumps(affected, ensure_ascii=False, indent=2)


def _format_references(value: Any) -> str:
    references = []
    for item in _as_list(value):
        if isinstance(item, dict):
            ref_type = item.get("type")
            url = item.get("url")
            if ref_type and url:
                references.append(f"- {ref_type}: {url}")
            elif url:
                references.append(f"- {url}")
        elif item:
            references.append(f"- {item}")
    return "\n".join(references)


def _field_start(text: str, field_name: str) -> int:
    match = re.search(rf'"{re.escape(field_name)}"\s*:', text)
    return match.start() if match else 0


def _field_end(text: str, field_name: str) -> int:
    start = _field_start(text, field_name)
    if start <= 0:
        return len(text)
    next_match = re.search(r'\n\s*"[^"]+"\s*:', text[start + 1 :])
    if next_match:
        return start + 1 + next_match.start()
    return len(text)


def _semgrep_metadata(rule: dict[str, Any]) -> dict[str, Any]:
    raw_metadata = rule.get("metadata") if isinstance(rule.get("metadata"), dict) else {}
    cwe = _metadata_values(raw_metadata, ("cwe", "cwe_id", "cwe_ids", "owasp"))
    languages = [str(item) for item in _as_list(rule.get("languages")) if str(item).strip()]
    return {
        "severity": str(rule.get("severity") or "").strip(),
        "message": str(rule.get("message") or "").strip(),
        "languages": languages,
        "cwe": cwe,
        "category": str(raw_metadata.get("category") or "").strip(),
        "technology": _metadata_values(raw_metadata, ("technology", "technologies")),
        "confidence": str(raw_metadata.get("confidence") or "").strip(),
        "likelihood": str(raw_metadata.get("likelihood") or "").strip(),
        "impact": str(raw_metadata.get("impact") or "").strip(),
    }


def _metadata_values(metadata: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = metadata.get(key)
        values.extend(str(item) for item in _as_list(value) if str(item).strip())
    return _dedupe_strings(values)


def _format_semgrep_header(*, rule_id: str, metadata: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Semgrep Rule",
            f"Rule ID: {rule_id}",
            f"Severity: {metadata.get('severity') or 'UNKNOWN'}",
            f"Languages: {', '.join(metadata.get('languages') or []) or 'UNKNOWN'}",
            f"CWE/OWASP: {', '.join(metadata.get('cwe') or []) or 'UNKNOWN'}",
            f"Category: {metadata.get('category') or 'UNKNOWN'}",
            f"Message: {metadata.get('message') or ''}",
        ]
    )


def _format_semgrep_rule(rule: dict[str, Any], *, include_patterns: bool = True) -> str:
    fields = {
        "message": rule.get("message"),
        "severity": rule.get("severity"),
        "languages": rule.get("languages"),
        "metadata": rule.get("metadata"),
    }
    if include_patterns:
        fields["patterns"] = _semgrep_pattern_payload(rule)
    return json.dumps(fields, ensure_ascii=False, indent=2, sort_keys=True)


def _format_semgrep_patterns(rule: dict[str, Any]) -> str:
    return json.dumps(_semgrep_pattern_payload(rule), ensure_ascii=False, indent=2, sort_keys=True)


def _semgrep_pattern_payload(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in rule.items()
        if key.startswith("pattern") or key in {"mode", "options", "fix", "fix-regex"}
    }


def _rule_start(text: str, rule_id: str) -> int:
    escaped = re.escape(rule_id)
    match = re.search(rf"(?m)^\s*-\s*id:\s*['\"]?{escaped}['\"]?\s*$", text)
    if match:
        return match.start()
    match = re.search(escaped, text)
    return match.start() if match else 0


def _dedupe_strings(items: Iterable[str]) -> list[str]:
    seen = set()
    output = []
    for item in items:
        normalized = str(item).strip()
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        output.append(normalized)
    return output


def chunk_stable_id(path: Path, start: int, end: int, text: str) -> str:
    digest = hashlib.sha1(f"v2:{path}:{start}:{end}:{text[:120]}".encode("utf-8")).hexdigest()
    return f"security-kb:{digest}"


def _point_id(record_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, record_id))


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _batches(items: list[KnowledgeChunk], size: int) -> Iterable[list[KnowledgeChunk]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return int(default)
    try:
        return int(value)
    except ValueError:
        return int(default)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return bool(default)
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _env_text(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()
