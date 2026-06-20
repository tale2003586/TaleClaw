from types import SimpleNamespace
import unittest

from memory.embeddings import HashEmbeddingProvider, SparseEmbedding
from knowledge.chunking.base import KnowledgeChunk
from knowledge.security_rag import SecurityKnowledgeIndex


class FakeEmbeddings:
    vector_size = 3

    def embed(self, text):
        return self.embed_dense(text)

    def embed_dense(self, text):
        return [1.0, 0.0, 0.0]

    def embed_sparse(self, text):
        return SparseEmbedding(indices=[7], values=[1.0])


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


class ReverseReranker:
    def rerank(self, query, candidates):
        return [
            (0.1 + index, candidate)
            for index, candidate in enumerate(candidates, start=1)
        ]


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

    def test_reranker_scores_replace_retrieval_scores(self) -> None:
        index = _hybrid_index(reranker=ReverseReranker())

        hits = index.search("SQL injection", top_k=1)

        self.assertEqual(1.1, hits[0].score)

    def test_delete_file_chunks_uses_source_path_filter(self) -> None:
        index = _hybrid_index()

        index.delete_file_chunks("/kb/a.md")

        delete_call = index._client.deletes[0]
        self.assertEqual("test", delete_call["collection_name"])
        self.assertIsNotNone(delete_call["points_selector"])


if __name__ == "__main__":
    unittest.main()
