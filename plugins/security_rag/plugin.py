from __future__ import annotations

import json

from knowledge.security_rag import build_security_index_from_env
from knowledge.tracing import Timer, make_rag_trace, write_rag_trace_if_enabled
from plugins.base import Plugin, ToolRegistration
from tools.schema import function_tool


class SecurityRagPlugin(Plugin):
    name = "security_rag"

    def tools(self) -> list[ToolRegistration]:
        return [
            ToolRegistration(
                schema=function_tool(
                    "security_rag_search",
                    "Search the local code security RAG knowledge base for secure coding, vulnerability, CWE/CVE, auth, injection, XSS, SSRF, token, and dependency security guidance.",
                    {
                        "query": {
                            "type": "string",
                            "description": "Security-focused search query. English or mixed Chinese/English usually works best.",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Number of chunks to return. Defaults to 5, maximum 10.",
                        },
                        "min_score": {
                            "type": "number",
                            "description": "Optional minimum vector similarity score.",
                        },
                    },
                    ["query"],
                ),
                handler=self.search,
                risk="low",
                enabled_modes={"bot", "coding"},
                always_on=False,
                source="plugin:security_rag",
            )
        ]

    def search(self, query: str, top_k: int = 5, min_score: float = 0.0) -> str:
        index = build_security_index_from_env()
        timer = Timer()
        search_trace = {}
        hits = index.search(
            query=query,
            top_k=max(1, min(10, int(top_k or 5))),
            min_score=float(min_score or 0.0),
            trace_callback=search_trace.update,
        )
        elapsed_ms = timer.ms()
        write_rag_trace_if_enabled(make_rag_trace(
            source="security_rag_search_tool",
            query=query,
            hits=hits,
            latency_ms={
                "search": elapsed_ms,
                **(search_trace.get("latency_ms") or {}),
                "total": elapsed_ms,
            },
        ))
        return json.dumps(
            [
                {
                    "score": hit.score,
                    "source": hit.source_relpath,
                    "title": hit.title,
                    "chunk_index": hit.chunk_index,
                    "text": hit.text,
                    "metadata": hit.metadata,
                }
                for hit in hits
            ],
            ensure_ascii=False,
            indent=2,
        )
