from types import SimpleNamespace
import unittest

from memory.embeddings import CachedEmbeddingProvider, HashEmbeddingProvider, SparseEmbedding
from memory.embeddings import BgeM3EmbeddingProvider
from knowledge.chunking.base import KnowledgeChunk
from knowledge.reranker import RerankerProvider
from knowledge.security_rag import SecurityKnowledgeIndex


class FakeEmbeddings:
    vector_size = 3

    def embed(self, text):
        return self.embed_dense(text)

    def embed_dense(self, text):
        return [1.0, 0.0, 0.0]

    def embed_sparse(self, text):
        return SparseEmbedding(indices=[7], values=[1.0])


class CountingEmbeddings(FakeEmbeddings):
    def __init__(self) -> None:
        self.dense_calls = 0
        self.sparse_calls = 0

    def embed_dense(self, text):
        self.dense_calls += 1
        return super().embed_dense(text)

    def embed_sparse(self, text):
        self.sparse_calls += 1
        return super().embed_sparse(text)


class FakeClient:
    def __init__(self) -> None:
        self.upserts = []
        self.queries = []
        self.deletes = []

    def upsert(self, *, collection_name, points):
        self.upserts.append((collection_name, points))

    def query_points(self, **kwargs):
        self.queries.append(kwargs)
        point = SimpleNamespace(
            id="p1",
            score=0.7,
            payload={
                "id": "chunk-1",
                "text": "SQL injection prepared statements",
                "source_path": "/kb/a.md",
                "source_relpath": "a.md",
                "title": "SQL",
                "chunk_index": 0,
                "metadata": {"severity": "HIGH"},
            },
        )
        return SimpleNamespace(points=[point])

    def delete(self, **kwargs):
        self.deletes.append(kwargs)


class MissingDenseVectorClient(FakeClient):
    def query_points(self, **kwargs):
        self.queries.append(kwargs)
        if "prefetch" in kwargs or kwargs.get("using") == "dense":
            raise RuntimeError("Wrong input: Not existing vector name error: dense")
        return super().query_points(**kwargs)


class ReverseReranker:
    def rerank(self, query, candidates):
        return [
            (0.1 + index, candidate)
            for index, candidate in enumerate(candidates, start=1)
        ]


class CapturingCrossEncoder:
    def __init__(self) -> None:
        self.pairs = []

    def compute_score(self, pairs, normalize=True):
        self.pairs = pairs
        return [0.9 for _ in pairs]


class FailingBgeModel:
    def encode(self, *args, **kwargs):
        raise IndexError("empty tokenizer batch")


def _hybrid_index(*, reranker=None):
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

    index = object.__new__(SecurityKnowledgeIndex)
    index.collection = "test"
    index.embeddings = FakeEmbeddings()
    index.hybrid_enabled = True
    index.reranker = reranker
    index.reranker_candidates = 30
    index._client = FakeClient()
    index._Distance = Distance
    index._VectorParams = VectorParams
    index._SparseVector = SparseVector
    index._SparseVectorParams = SparseVectorParams
    index._Prefetch = Prefetch
    index._Fusion = Fusion
    index._FusionQuery = FusionQuery
    index._Filter = Filter
    index._FieldCondition = FieldCondition
    index._MatchValue = MatchValue
    index._MatchAny = MatchAny
    return index


class SecurityRagHybridTests(unittest.TestCase):
    def test_hash_embedding_sparse_is_non_empty(self) -> None:
        provider = HashEmbeddingProvider(dimensions=16)

        sparse = provider.embed_sparse("SQL injection SQL")

        self.assertTrue(sparse.indices)
        self.assertEqual(len(sparse.indices), len(sparse.values))

    def test_cached_embedding_provider_reuses_dense_and_sparse_vectors(self) -> None:
        inner = CountingEmbeddings()
        provider = CachedEmbeddingProvider(inner, max_size=4, ttl_seconds=60)

        self.assertEqual(provider.embed_dense("same"), provider.embed_dense("same"))
        self.assertEqual(provider.embed_sparse("same").indices, provider.embed_sparse("same").indices)

        self.assertEqual(1, inner.dense_calls)
        self.assertEqual(1, inner.sparse_calls)

    def test_bge_m3_dense_falls_back_when_flagembedding_batch_is_empty(self) -> None:
        provider = object.__new__(BgeM3EmbeddingProvider)
        provider.model_name = "fake"
        provider._dimensions = 16
        provider._max_length = 128
        provider.devices = "cuda:0"
        import threading

        provider._lock = threading.RLock()
        provider._model = FailingBgeModel()

        vector = provider.embed_dense("authorization bypass")

        self.assertEqual(16, len(vector))

    def test_upsert_uses_named_dense_and_sparse_vectors(self) -> None:
        index = _hybrid_index()
        chunk = KnowledgeChunk(
            id="chunk-1",
            text="SQL injection",
            source_path="/kb/a.md",
            source_relpath="a.md",
            title="SQL",
            chunk_index=0,
            char_start=0,
            char_end=13,
            source_type="markdown",
            metadata={},
        )

        indexed = index.upsert_chunks([chunk])

        self.assertEqual(1, indexed)
        point = index._client.upserts[0][1][0]
        self.assertIn("dense", point.vector)
        self.assertIn("sparse", point.vector)
        self.assertEqual([7], point.vector["sparse"].indices)

    def test_hybrid_search_uses_rrf_prefetch_and_trace_callback(self) -> None:
        index = _hybrid_index()
        traces = []

        hits = index.search("SQL injection", top_k=1, trace_callback=traces.append)

        self.assertEqual("chunk-1", hits[0].id)
        query = index._client.queries[0]
        self.assertEqual(2, len(query["prefetch"]))
        self.assertEqual("dense", query["prefetch"][0].using)
        self.assertEqual("sparse", query["prefetch"][1].using)
        self.assertTrue(traces)
        self.assertTrue(traces[0]["hybrid_enabled"])
        self.assertEqual("hybrid", traces[0]["retrieval_mode"])

    def test_hybrid_index_can_force_dense_search(self) -> None:
        index = _hybrid_index()
        traces = []

        hits = index.search("SQL injection", top_k=1, retrieval_mode="dense", trace_callback=traces.append)

        self.assertEqual("chunk-1", hits[0].id)
        query = index._client.queries[0]
        self.assertNotIn("prefetch", query)
        self.assertEqual("dense", query["using"])
        self.assertEqual("dense", traces[0]["retrieval_mode"])

    def test_dense_search_falls_back_to_unnamed_vector_when_dense_name_missing(self) -> None:
        index = _hybrid_index()
        index._client = MissingDenseVectorClient()

        hits = index.search("SQL injection", top_k=1, retrieval_mode="dense")

        self.assertEqual("chunk-1", hits[0].id)
        self.assertEqual("dense", index._client.queries[0]["using"])
        self.assertIsNone(index._client.queries[1]["using"])

    def test_hybrid_search_falls_back_to_unnamed_dense_when_collection_is_not_hybrid(self) -> None:
        index = _hybrid_index()
        index._client = MissingDenseVectorClient()
        traces = []

        hits = index.search("SQL injection", top_k=1, trace_callback=traces.append)

        self.assertEqual("chunk-1", hits[0].id)
        self.assertIn("prefetch", index._client.queries[0])
        self.assertEqual("dense", index._client.queries[1]["using"])
        self.assertIsNone(index._client.queries[2]["using"])
        self.assertIn("hybrid_fallback_dense", traces[0]["latency_ms"])

    def test_reranker_scores_replace_retrieval_scores(self) -> None:
        index = _hybrid_index(reranker=ReverseReranker())

        hits = index.search("SQL injection", top_k=1)

        self.assertEqual(1.1, hits[0].score)

    def test_reranker_truncates_candidate_text(self) -> None:
        model = CapturingCrossEncoder()
        reranker = object.__new__(RerankerProvider)
        reranker.model_name = "fake"
        reranker.max_chars = 8
        reranker._model = model
        candidate = SimpleNamespace(text="x" * 100)

        reranker.rerank("query", [candidate])

        self.assertEqual("x" * 8, model.pairs[0][1])

    def test_delete_file_chunks_uses_source_path_filter(self) -> None:
        index = _hybrid_index()

        index.delete_file_chunks("/kb/a.md")

        delete_call = index._client.deletes[0]
        self.assertEqual("test", delete_call["collection_name"])
        self.assertIsNotNone(delete_call["points_selector"])


if __name__ == "__main__":
    unittest.main()
