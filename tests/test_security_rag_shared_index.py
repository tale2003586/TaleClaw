from dataclasses import replace
import json
from unittest.mock import patch

from knowledge.caching import CachedSecurityIndex
from knowledge.security_rag import KnowledgeHit
from plugins.security_rag.plugin import SecurityRagPlugin


class MutableIndex:
    collection = "app-security-kb"

    def __init__(self):
        self.calls = 0
        self.hit = KnowledgeHit(
            id="sql-guidance", text="old evidence", score=0.9,
            source_path="/kb/sql.md", source_relpath="sql.md", title="SQL",
            chunk_index=0, metadata={},
        )

    def search(self, query, **kwargs):
        self.calls += 1
        return [self.hit]


def test_shared_index_tool_bypasses_stale_context_results(monkeypatch):
    monkeypatch.setenv("SECURITY_RAG_TRACE_ENABLED", "0")
    source = MutableIndex()
    shared = CachedSecurityIndex(source, ttl_seconds=3600)
    assert shared.search("sql injection", top_k=5, min_score=0.0)[0].text == "old evidence"
    source.hit = replace(source.hit, text="updated evidence")
    assert shared.search("sql injection", top_k=5, min_score=0.0)[0].text == "old evidence"

    with patch(
        "plugins.security_rag.plugin.build_security_index_from_env",
        side_effect=AssertionError("the app index must be reused"),
    ):
        plugin = SecurityRagPlugin(index=shared)
        first = json.loads(plugin.search("sql injection"))
        source.hit = replace(source.hit, text="latest evidence")
        second = json.loads(plugin.search("sql injection"))

    assert first[0]["text"] == "updated evidence"
    assert second[0]["text"] == "latest evidence"
    assert source.calls == 3


def test_tool_only_plugin_keeps_independent_index_fallback(monkeypatch):
    monkeypatch.setenv("SECURITY_RAG_TRACE_ENABLED", "0")
    source = MutableIndex()
    with patch(
        "plugins.security_rag.plugin.build_security_index_from_env",
        return_value=source,
    ) as build_index:
        output = json.loads(SecurityRagPlugin().search("sql injection"))

    build_index.assert_called_once_with()
    assert output[0]["text"] == "old evidence"
