import json
import tempfile
import unittest
from pathlib import Path

from knowledge.caching import CachedSecurityIndex
from knowledge.tracing import RagTraceStore, make_rag_trace


class FakeIndex:
    collection = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def search(self, query: str, **kwargs):
        self.calls += 1
        return [{"query": query, "kwargs": kwargs}]


class SecurityRagObservabilityTests(unittest.TestCase):
    def test_cached_security_index_reuses_same_query(self) -> None:
        fake = FakeIndex()
        cached = CachedSecurityIndex(fake, max_size=2, ttl_seconds=60)

        first = cached.search("sql injection", top_k=5)
        second = cached.search("sql injection", top_k=5)
        third = cached.search("sql injection", top_k=6)

        self.assertEqual(first, second)
        self.assertEqual(2, fake.calls)
        self.assertNotEqual(second, third)

    def test_rag_trace_store_writes_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RagTraceStore(tmp)
            trace = make_rag_trace(
                source="test",
                query="sql injection",
                hits=[],
                latency_ms={"total": 1.2},
            )

            path = store.write(trace)

            payload = json.loads(Path(path).read_text(encoding="utf-8").strip())
        self.assertEqual("test", payload["source"])
        self.assertEqual("sql injection", payload["query"])
        self.assertEqual(1.2, payload["latency_ms"]["total"])


if __name__ == "__main__":
    unittest.main()
