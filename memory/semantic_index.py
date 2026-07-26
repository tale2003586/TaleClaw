from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, Sequence

from memory.domain import MemoryItem, OwnerKey


@dataclass(frozen=True)
class IndexedMemoryHit:
    memory_id: str
    memory_version: int
    score: float
    owner_scope: str
    owner_id: str
    kind: str


class SemanticMemoryIndex(Protocol):
    def upsert(self, item: MemoryItem) -> None: ...

    def delete(self, memory_id: str, version: int | None = None) -> None: ...

    def search(
        self,
        query: str,
        scopes: Sequence[OwnerKey],
        top_k: int,
    ) -> list[IndexedMemoryHit]: ...


class NullSemanticMemoryIndex:
    def upsert(self, item: MemoryItem) -> None:
        return None

    def delete(self, memory_id: str, version: int | None = None) -> None:
        return None

    def search(
        self,
        query: str,
        scopes: Sequence[OwnerKey],
        top_k: int,
    ) -> list[IndexedMemoryHit]:
        return []


class QdrantSemanticMemoryIndex:
    """Derived Qdrant index; payload deliberately excludes evidence and source text."""

    def __init__(
        self,
        *,
        url: str,
        collection: str,
        embeddings,
        api_key: str | None = None,
        distance: str = "Cosine",
        client=None,
    ) -> None:
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams
        except ImportError as exc:
            raise RuntimeError("qdrant-client is required for semantic memory indexing.") from exc
        self.collection = collection
        self.embeddings = embeddings
        self._client = client or QdrantClient(
            url=url,
            api_key=api_key or None,
            check_compatibility=False,
            trust_env=False,
        )
        self._Distance = Distance
        self._VectorParams = VectorParams
        self._ensure_collection(distance)

    def upsert(self, item: MemoryItem) -> None:
        from qdrant_client.models import PointStruct

        payload = {
            "memory_id": item.id,
            "memory_version": item.version,
            "owner_scope": item.owner_scope.value,
            "owner_id": item.owner_id,
            "kind": item.kind.value,
            "status": item.status.value,
            "valid_until": item.valid_until.isoformat() if item.valid_until else None,
            "content_digest": hashlib.sha256(item.content.encode()).hexdigest(),
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }
        self._client.upsert(
            collection_name=self.collection,
            points=[PointStruct(
                id=_semantic_point_id(item.id, item.version),
                vector=self.embeddings.embed(item.content),
                payload=payload,
            )],
        )

    def delete(self, memory_id: str, version: int | None = None) -> None:
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            FilterSelector,
            MatchValue,
        )

        must = [FieldCondition(key="memory_id", match=MatchValue(value=memory_id))]
        if version is not None:
            must.append(FieldCondition(
                key="memory_version",
                match=MatchValue(value=int(version)),
            ))
        self._client.delete(
            collection_name=self.collection,
            points_selector=FilterSelector(filter=Filter(must=must)),
        )

    def search(
        self,
        query: str,
        scopes: Sequence[OwnerKey],
        top_k: int,
    ) -> list[IndexedMemoryHit]:
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            MatchValue,
        )

        owners = list(scopes)
        if not owners or not str(query).strip():
            return []
        owner_filters = [
            Filter(must=[
                FieldCondition(key="owner_scope", match=MatchValue(value=owner.scope.value)),
                FieldCondition(key="owner_id", match=MatchValue(value=owner.id)),
            ])
            for owner in owners
        ]
        query_filter = Filter(
            must=[FieldCondition(key="status", match=MatchValue(value="active"))],
            should=owner_filters,
        )
        points = self._query_points(
            vector=self.embeddings.embed(query),
            query_filter=query_filter,
            limit=max(1, int(top_k)),
        )
        hits = []
        for point in points:
            payload = getattr(point, "payload", {}) or {}
            hits.append(IndexedMemoryHit(
                memory_id=str(payload.get("memory_id") or ""),
                memory_version=int(payload.get("memory_version") or 0),
                score=float(getattr(point, "score", 0.0) or 0.0),
                owner_scope=str(payload.get("owner_scope") or ""),
                owner_id=str(payload.get("owner_id") or ""),
                kind=str(payload.get("kind") or ""),
            ))
        return [hit for hit in hits if hit.memory_id and hit.memory_version > 0]

    def _ensure_collection(self, distance: str) -> None:
        if hasattr(self._client, "collection_exists") and self._client.collection_exists(self.collection):
            return
        if not hasattr(self._client, "collection_exists"):
            try:
                self._client.get_collection(self.collection)
                return
            except Exception:
                pass
        self._client.create_collection(
            collection_name=self.collection,
            vectors_config=self._VectorParams(
                size=self.embeddings.vector_size,
                distance=self._distance(distance),
            ),
        )

    def _distance(self, value: str):
        normalized = str(value or "Cosine").strip().upper()
        if normalized == "DOT":
            return self._Distance.DOT
        if normalized in {"EUCLID", "EUCLIDEAN"}:
            return self._Distance.EUCLID
        if normalized == "MANHATTAN":
            return self._Distance.MANHATTAN
        return self._Distance.COSINE

    def _query_points(self, *, vector, query_filter, limit: int):
        if hasattr(self._client, "query_points"):
            response = self._client.query_points(
                collection_name=self.collection,
                query=vector,
                query_filter=query_filter,
                with_payload=True,
                limit=limit,
            )
            return list(getattr(response, "points", []) or [])
        return list(self._client.search(
            collection_name=self.collection,
            query_vector=vector,
            query_filter=query_filter,
            with_payload=True,
            limit=limit,
        ))


def _semantic_point_id(memory_id: str, version: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"taleclaw-semantic:{memory_id}:{version}"))
