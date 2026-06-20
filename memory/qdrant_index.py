from __future__ import annotations

from dataclasses import asdict
import uuid

from memory.embeddings import EmbeddingProvider
from memory.vector_index import MemoryHit, MemoryRecord


class QdrantMemoryVectorIndex:
    def __init__(
        self,
        *,
        url: str,
        collection: str,
        embeddings: EmbeddingProvider,
        api_key: str | None = None,
        distance: str = "Cosine",
    ) -> None:
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams
        except ImportError as exc:
            raise RuntimeError(
                "qdrant-client is required when HISTORY_VECTOR_BACKEND=qdrant. "
                "Install requirements.txt or disable HISTORY_VECTOR_ENABLED."
            ) from exc

        self.collection = collection
        self.embeddings = embeddings
        self._client = QdrantClient(
            url=url,
            api_key=api_key or None,
            check_compatibility=False,
            trust_env=False,
        )
        self._Distance = Distance
        self._VectorParams = VectorParams
        self._ensure_collection(distance=distance)

    def upsert(self, record: MemoryRecord) -> None:
        from qdrant_client.models import PointStruct

        vector = self.embeddings.embed(record.text)
        payload = asdict(record)
        payload["text"] = record.text
        self._client.upsert(
            collection_name=self.collection,
            points=[
                PointStruct(
                    id=_point_id(record.id),
                    vector=vector,
                    payload=payload,
                )
            ],
        )

    def search(
        self,
        *,
        query: str,
        scope: str,
        top_k: int,
        min_score: float = 0.0,
    ) -> list[MemoryHit]:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        vector = self.embeddings.embed(query)
        query_filter = Filter(
            must=[
                FieldCondition(
                    key="scope",
                    match=MatchValue(value=scope),
                )
            ]
        )
        points = self._query_points(
            vector=vector,
            query_filter=query_filter,
            limit=max(1, int(top_k)),
        )
        hits = []
        for point in points:
            score = float(getattr(point, "score", 0.0) or 0.0)
            if score < float(min_score):
                continue
            payload = getattr(point, "payload", {}) or {}
            hits.append(
                MemoryHit(
                    id=str(payload.get("id") or getattr(point, "id", "")),
                    text=str(payload.get("text") or ""),
                    score=score,
                    scope=str(payload.get("scope") or scope),
                    source_type=str(payload.get("source_type") or ""),
                    source_ref=str(payload.get("source_ref") or ""),
                    metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
                )
            )
        return hits

    def _ensure_collection(self, *, distance: str) -> None:
        if self._collection_exists():
            return
        self._client.create_collection(
            collection_name=self.collection,
            vectors_config=self._VectorParams(
                size=self.embeddings.vector_size,
                distance=self._distance(distance),
            ),
        )

    def _collection_exists(self) -> bool:
        if hasattr(self._client, "collection_exists"):
            return bool(self._client.collection_exists(self.collection))
        try:
            self._client.get_collection(self.collection)
            return True
        except Exception:
            return False

    def _distance(self, value: str):
        normalized = str(value or "Cosine").strip().upper()
        if normalized == "DOT":
            return self._Distance.DOT
        if normalized in {"EUCLID", "EUCLIDEAN"}:
            return self._Distance.EUCLID
        if normalized == "MANHATTAN":
            return self._Distance.MANHATTAN
        return self._Distance.COSINE

    def _query_points(self, *, vector: list[float], query_filter, limit: int):
        if hasattr(self._client, "query_points"):
            response = self._client.query_points(
                collection_name=self.collection,
                query=vector,
                query_filter=query_filter,
                with_payload=True,
                limit=limit,
            )
            return list(getattr(response, "points", []) or [])
        return list(
            self._client.search(
                collection_name=self.collection,
                query_vector=vector,
                query_filter=query_filter,
                with_payload=True,
                limit=limit,
            )
        )


def _point_id(record_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"taleclaw-memory:{record_id}"))
