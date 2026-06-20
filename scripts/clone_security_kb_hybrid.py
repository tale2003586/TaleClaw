from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from knowledge.security_rag import (
    DEFAULT_COLLECTION,
    build_security_embedding_provider_from_env,
    build_security_sparse_embedding_provider_from_env,
)
from memory.embeddings import EmbeddingProvider, HashEmbeddingProvider, SparseEmbedding
from runtime.env_loader import load_dotenv_file


def main() -> int:
    load_dotenv_file(ROOT / ".env")
    parser = argparse.ArgumentParser(
        description=(
            "Clone an existing dense security RAG Qdrant collection into a "
            "new hybrid dense+sparse collection without re-slicing or "
            "recomputing dense embeddings."
        )
    )
    parser.add_argument(
        "--source-collection",
        default=os.getenv("SECURITY_RAG_COLLECTION", DEFAULT_COLLECTION),
        help="Existing dense or named-vector collection to clone from.",
    )
    parser.add_argument(
        "--target-collection",
        default=None,
        help="New hybrid collection. Defaults to <source>_hybrid.",
    )
    parser.add_argument(
        "--sparse-provider",
        choices=["hash", "env"],
        default="hash",
        help=(
            "Sparse vector provider. 'hash' is fast and offline; 'env' uses "
            "SECURITY_RAG_SPARSE_EMBEDDING_PROVIDER or the dense provider."
        ),
    )
    parser.add_argument("--recreate", action="store_true", help="Delete target collection first if it exists.")
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--limit-points", type=int, default=None, help="Smoke-test limit.")
    parser.add_argument("--progress-every", type=int, default=1000)
    args = parser.parse_args()

    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct, SparseVector, SparseVectorParams, VectorParams

    source = str(args.source_collection or DEFAULT_COLLECTION)
    target = str(args.target_collection or f"{source}_hybrid")
    if source == target:
        raise SystemExit("source and target collections must be different")

    client = QdrantClient(
        url=os.getenv("SECURITY_RAG_QDRANT_URL", os.getenv("QDRANT_URL", "http://127.0.0.1:6333")).strip(),
        api_key=os.getenv("SECURITY_RAG_QDRANT_API_KEY", os.getenv("QDRANT_API_KEY", "")).strip() or None,
        check_compatibility=False,
        trust_env=False,
    )
    sparse_provider = _build_sparse_provider(args.sparse_provider)

    source_info = client.get_collection(source)
    vector_size, distance = _source_dense_config(source_info)
    if vector_size <= 0:
        raise SystemExit(f"Cannot determine dense vector size for collection: {source}")

    if _collection_exists(client, target):
        if not args.recreate:
            print(json.dumps({
                "event": "target_collection_exists",
                "target_collection": target,
                "message": "continuing with upserts; pass --recreate to rebuild from scratch",
            }, ensure_ascii=False), flush=True)
        else:
            client.delete_collection(target)

    if not _collection_exists(client, target):
        client.create_collection(
            collection_name=target,
            vectors_config={
                "dense": VectorParams(size=vector_size, distance=distance),
            },
            sparse_vectors_config={
                "sparse": SparseVectorParams(),
            },
        )

    started = time.perf_counter()
    offset = None
    seen = 0
    upserted = 0
    skipped_no_dense = 0
    batch_size = max(1, int(args.batch_size or 128))
    limit_points = args.limit_points if args.limit_points and args.limit_points > 0 else None

    while True:
        remaining = None if limit_points is None else max(0, limit_points - seen)
        if remaining == 0:
            break
        limit = batch_size if remaining is None else min(batch_size, remaining)
        records, offset = client.scroll(
            collection_name=source,
            limit=limit,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        if not records:
            break

        points = []
        for record in records:
            seen += 1
            dense = _dense_vector_from_record(record)
            if not dense:
                skipped_no_dense += 1
                continue
            payload = dict(getattr(record, "payload", {}) or {})
            text = _payload_text(payload)
            sparse = sparse_provider.embed_sparse(text)
            points.append(
                PointStruct(
                    id=getattr(record, "id"),
                    vector={
                        "dense": dense,
                        "sparse": _to_qdrant_sparse(SparseVector, sparse),
                    },
                    payload=payload,
                )
            )

        if points:
            client.upsert(collection_name=target, points=points, wait=True)
            upserted += len(points)

        if args.progress_every and seen % max(1, int(args.progress_every)) < batch_size:
            print(json.dumps({
                "event": "progress",
                "source_collection": source,
                "target_collection": target,
                "seen": seen,
                "upserted": upserted,
                "skipped_no_dense": skipped_no_dense,
                "elapsed_seconds": round(time.perf_counter() - started, 2),
            }, ensure_ascii=False), flush=True)

        if offset is None:
            break

    print(json.dumps({
        "event": "clone_completed",
        "source_collection": source,
        "target_collection": target,
        "dense_vector_size": vector_size,
        "distance": str(getattr(distance, "value", distance)),
        "sparse_provider": args.sparse_provider,
        "seen": seen,
        "upserted": upserted,
        "skipped_no_dense": skipped_no_dense,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "next_env": {
            "SECURITY_RAG_COLLECTION": target,
            "SECURITY_RAG_HYBRID_ENABLED": "1",
            "SECURITY_RAG_SPARSE_EMBEDDING_PROVIDER": args.sparse_provider if args.sparse_provider == "hash" else "",
        },
    }, ensure_ascii=False, indent=2))
    return 0


def _build_sparse_provider(choice: str) -> EmbeddingProvider:
    if choice == "env":
        return build_security_sparse_embedding_provider_from_env() or build_security_embedding_provider_from_env()
    dimensions = _env_int("SECURITY_RAG_VECTOR_SIZE", _env_int("QDRANT_VECTOR_SIZE", 512))
    return HashEmbeddingProvider(
        dimensions=dimensions,
        sparse_dimensions=_env_int("SECURITY_RAG_SPARSE_HASH_SIZE", 1_048_576),
    )


def _collection_exists(client: Any, collection: str) -> bool:
    if hasattr(client, "collection_exists"):
        return bool(client.collection_exists(collection))
    try:
        client.get_collection(collection)
        return True
    except Exception:
        return False


def _source_dense_config(collection_info: Any) -> tuple[int, Any]:
    params = getattr(getattr(collection_info, "config", None), "params", None)
    vectors = getattr(params, "vectors", None)
    if isinstance(vectors, dict):
        config = vectors.get("dense") or next(iter(vectors.values()), None)
    else:
        config = vectors
    size = int(_field(config, "size", 0) or 0)
    distance = _normalize_distance(_field(config, "distance", "Cosine"))
    return size, distance


def _normalize_distance(value: Any):
    from qdrant_client.models import Distance

    raw = str(getattr(value, "value", value) or "Cosine").strip().lower()
    if raw == "dot":
        return Distance.DOT
    if raw in {"euclid", "euclidean"}:
        return Distance.EUCLID
    if raw == "manhattan":
        return Distance.MANHATTAN
    return Distance.COSINE


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _dense_vector_from_record(record: Any) -> list[float]:
    vector = getattr(record, "vector", None)
    if isinstance(vector, dict):
        dense = vector.get("dense")
        if dense is None:
            dense = next((_value for _value in vector.values() if _looks_like_dense(_value)), None)
    else:
        dense = vector
    if hasattr(dense, "tolist"):
        dense = dense.tolist()
    if not _looks_like_dense(dense):
        return []
    return [float(value) for value in dense]


def _looks_like_dense(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and (not value or isinstance(value[0], (int, float)))


def _payload_text(payload: dict[str, Any]) -> str:
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text
    parts = [
        payload.get("title"),
        payload.get("source_relpath"),
        payload.get("source_path"),
        payload.get("source_type"),
    ]
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        parts.extend(str(value) for value in metadata.values() if isinstance(value, (str, int, float)))
    return "\n".join(str(part) for part in parts if part)


def _to_qdrant_sparse(sparse_cls: Any, sparse: SparseEmbedding):
    return sparse_cls(indices=sparse.indices, values=sparse.values)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return int(default)
    try:
        return int(value)
    except ValueError:
        return int(default)


if __name__ == "__main__":
    raise SystemExit(main())
