import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from knowledge.caching import CachedSecurityIndex
from knowledge.security_rag import KnowledgeHit
from knowledge.tracing import RagTraceStore, make_rag_trace
from plugins.security_rag.plugin import SecurityRagPlugin
from runtime.context import ContextBuilder
from runtime.context.providers import DEFAULT_CONTEXT_PROVIDERS
from runtime.context.retrieval import ContextRetrievalService
from runtime.trace.summary import build_trace_summary_payload
from tools.schema import function_tool
from tools.tool_registry import ToolRegistry
from tools.spec import ToolExposure, ToolSpec


class FakeIndex:
    collection = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def search(self, query: str, **kwargs):
        self.calls += 1
        return [{"query": query, "kwargs": kwargs}]


class FakeRuntimeTraceStore:
    def __init__(self) -> None:
        self.events = []

    def append_event(self, run_state, event_name, payload, **kwargs) -> None:
        self.events.append({
            "event": event_name,
            "payload": payload,
            **kwargs,
        })


class FakeRuntimeIndex:
    def search(self, query: str, **kwargs):
        callback = kwargs.get("trace_callback")
        if callback is not None:
            callback({
                "query": query,
                "retrieval_mode": "dense",
                "hybrid_enabled": False,
                "reranker_enabled": False,
                "candidate_count": 1,
                "final_count": 1,
                "latency_ms": {"dense_search": 1.0, "total": 1.2},
            })
        return [
            KnowledgeHit(
                id="hit-1",
                text="Use prepared statements.",
                score=0.88,
                source_path="/kb/sql.md",
                source_relpath="sql.md",
                title="SQL",
                chunk_index=0,
                metadata={"cwe": "CWE-89"},
            )
        ]


class FakeDecision:
    use_rag = True
    query = "sql injection prepared statements"
    route = "keyword"
    confidence = 0.9
    reason = "matched keyword"
    top_k = 5
    min_score = 0.0

    def to_dict(self) -> dict:
        return {
            "use_rag": self.use_rag,
            "query": self.query,
            "route": self.route,
            "confidence": self.confidence,
            "reason": self.reason,
        }


class FakeRouter:
    def route_with_retrieval(self, query, *, index, trace_callback=None, **kwargs):
        hits = index.search(
            query=query,
            top_k=5,
            trace_callback=lambda payload: trace_callback({
                "event": "security.rag.search.completed",
                "payload": {
                    "stage": "fast_dense",
                    "query": query,
                    "retrieval_mode": payload.get("retrieval_mode"),
                    "reranker_enabled": payload.get("reranker_enabled"),
                    "hit_count": payload.get("final_count"),
                    "tier": "high",
                    "top_score": 0.88,
                    "latency_ms": payload.get("latency_ms"),
                },
            }) if trace_callback is not None else None,
        )
        return SimpleNamespace(
            decision=FakeDecision(),
            hits=hits,
            action="direct",
            reason="fast dense",
            searches=[],
        )


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

    def test_security_rag_tool_writes_runtime_trace_events(self) -> None:
        trace_store = FakeRuntimeTraceStore()
        run_state = SimpleNamespace(run_id="run-test", session_id="session-test")

        with patch(
            "plugins.security_rag.plugin.build_security_index_from_env",
            return_value=FakeRuntimeIndex(),
        ):
            output = SecurityRagPlugin().search(
                "sql injection",
                _trace_store=trace_store,
                _run_state=run_state,
                _parent_span_id="run-test:tool:1",
            )

        event_names = [event["event"] for event in trace_store.events]
        self.assertIn("security.rag.started", event_names)
        self.assertIn("security.rag.search.completed", event_names)
        self.assertIn("security.rag.completed", event_names)
        completed = next(event for event in trace_store.events if event["event"] == "security.rag.completed")
        self.assertEqual(1, completed["payload"]["hit_count"])
        self.assertIn("prepared statements", output)

    def test_context_auto_security_rag_writes_runtime_trace_events(self) -> None:
        trace_store = FakeRuntimeTraceStore()
        run_state = SimpleNamespace(run_id="run-test", session_id="session-test")
        session = SimpleNamespace(
            id="session-test",
            messages=[{"role": "user", "content": "SQL 注入怎么修？"}],
            metadata={},
        )

        ContextBuilder(
            context_providers=DEFAULT_CONTEXT_PROVIDERS,
            retrieval_service=ContextRetrievalService(
                security_retrieval_router=FakeRouter(),
                security_knowledge_index=FakeRuntimeIndex(),
            ),
        ).build(
            session=session,
            agent_spec=SimpleNamespace(instructions="base", tool_mode="coding"),
            trace_store=trace_store,
            run_state=run_state,
            trace_parent_span_id="run-test:context:1",
            reasoning_step=1,
        )

        event_names = [event["event"] for event in trace_store.events]
        self.assertIn("security.rag.started", event_names)
        self.assertIn("security.rag.search.completed", event_names)
        self.assertIn("security.rag.completed", event_names)
        completed = next(event for event in trace_store.events if event["event"] == "security.rag.completed")
        self.assertEqual("context_auto", completed["payload"]["source"])
        self.assertEqual("direct", completed["payload"]["action"])

    def test_trace_summary_includes_security_rag_summary(self) -> None:
        summary = build_trace_summary_payload(
            run_state={"run_id": "run-test", "session_id": "session-test", "status": "completed"},
            metrics={},
            report={},
            events=[
                {
                    "event": "security.rag.started",
                    "payload": {"source": "context_auto", "query": "SQL 注入怎么修？"},
                },
                {
                    "event": "security.rag.search.completed",
                    "payload": {"stage": "fast_dense", "reranker_enabled": True},
                },
                {
                    "event": "security.rag.completed",
                    "payload": {
                        "source": "context_auto",
                        "query": "SQL 注入怎么修？",
                        "query_rewritten": True,
                        "action": "direct",
                        "route": "keyword",
                        "hit_count": 2,
                        "latency_ms": {"total": 12.5},
                    },
                },
            ],
        )

        rag = summary["security_rag"]
        self.assertEqual(1, rag["requests"])
        self.assertEqual(1, rag["completed"])
        self.assertEqual(1, rag["searches"])
        self.assertEqual(1, rag["rerank_searches"])
        self.assertEqual(1, rag["rewrite_count"])
        self.assertEqual(2, rag["hit_count"])

    def test_tool_registry_passes_runtime_trace_kwargs_to_handlers(self) -> None:
        registry = ToolRegistry()

        def handler(_trace_store=None, _run_state=None, _parent_span_id=None):
            return json.dumps({
                "trace_store": _trace_store,
                "run_state": _run_state,
                "parent_span_id": _parent_span_id,
            })

        registry.register(ToolSpec(
            schema=function_tool("trace_probe", "Trace probe.", {}, []),
            handler=handler,
            allowed_modes=frozenset({"coding"}),
            exposure=ToolExposure.PRELOADED,
            runtime_parameters=frozenset({
                "_trace_store", "_run_state", "_parent_span_id",
            }),
        ))

        output = registry.execute(
            "trace_probe",
            {},
            mode="coding",
            trace_store="store",
            run_state="run",
            parent_span_id="parent",
        )
        payload = json.loads(output)
        self.assertEqual("store", payload["trace_store"])
        self.assertEqual("run", payload["run_state"])
        self.assertEqual("parent", payload["parent_span_id"])


if __name__ == "__main__":
    unittest.main()
