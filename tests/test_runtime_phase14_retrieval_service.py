from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from runtime.context import ContextBuilder
from runtime.context.providers import DEFAULT_CONTEXT_PROVIDERS
from runtime.context.retrieval import ContextRetrievalService
from runtime.sessions import Session


ROOT = Path(__file__).resolve().parents[1]


class _Hit:
    score = 0.9
    source_type = "session_turn"
    source_ref = "session:1"
    metadata = {"message_count": 2, "session_id": "phase14:history"}
    text = "remembered result"


class _HistoryIndex:
    def search(self, **kwargs):
        return [_Hit()]


def test_builder_no_longer_implements_retrieval_or_security_trace():
    source = (ROOT / "runtime/context/builder.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    method_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "_build_retrieved_history_block" not in method_names
    assert "_build_security_knowledge_block" not in method_names
    assert "knowledge.tracing" not in source
    assert "runtime.trace.rag" not in source
    assert "SECURITY_RAG_" not in source


def test_explicit_retrieval_service_preserves_history_context():
    service = ContextRetrievalService(
        history_vector_index=_HistoryIndex(),
        history_scope_resolver=lambda session: f"scope:{session.id}",
    )
    builder = ContextBuilder(
        context_providers=DEFAULT_CONTEXT_PROVIDERS,
        retrieval_service=service,
    )
    session = Session(id="phase14:history")
    session.add_message("user", "find prior result")

    context = builder.build(
        session=session,
        agent_spec=SimpleNamespace(instructions="system", tool_mode="bot"),
    )

    assert "<episodic_history" in context.messages[-2]["content"]
    assert "remembered result" in context.messages[-2]["content"]


def test_minimal_builder_has_no_retrieval_service():
    assert ContextBuilder().retrieval_service is None
