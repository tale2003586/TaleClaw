#!/usr/bin/env python3
"""Deterministic, offline Phase 0 runtime micro-benchmarks."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fakes import make_agent_spec
from applications.coding.session import TaskSessionFactory
from applications.coding.context_state import build_coding_context_view
from agents.subagent.runner import TaskSubagentRunner
from runtime.context import (
    ContextBuilder,
    ContextBundle,
    ContextMemoryService,
    PromptAssetsService,
)
from runtime.context.budget import ContextBudgeter
from runtime.context.providers import DEFAULT_CONTEXT_PROVIDERS
from runtime.runtime import Runtime
from runtime.agent_spec import AgentSpec
from runtime.runtime import RunContext, Runtime
from runtime.token_estimator import estimate_tokens
from runtime.trace.run_state import RunState
from runtime.trace.trace_store import TraceStore
from memory.vector_index import MemoryHit, MemoryRecord
from runtime.sessions import Session
from tests.fakes.fake_tools import RecordingTool, registry_with_tool
from tests.fakes.scripted_model import FinalResponse, ScriptedModel, ToolResponse
from tools.executor import ToolExecutionRequest, ToolExecutor


DEFAULT_OUTPUT = ROOT / "benchmarks" / "results" / "runtime_phase0.json"
PROFILE = make_agent_spec("phase0", "offline baseline prompt", "bot")


class InMemorySessions:
    def __init__(self):
        self.sessions = {}

    def get_or_create(self, session_id):
        self.sessions.setdefault(session_id, Session(id=session_id))
        return self.sessions[session_id]


class MemoryStore:
    def __init__(self, text):
        self.text = text

    def read_all(self):
        return self.text


class NullTrace:
    def append_event(self, run_state, event_name, payload, **kwargs):
        return None

    def write_run_state(self, run_state):
        return None


class DeterministicVectorIndex:
    def __init__(self):
        self.records = {}

    def upsert(self, record: MemoryRecord):
        self.records[record.id] = record

    def search(self, *, query, scope, top_k, min_score=0.0):
        query_terms = set(query.lower().split())
        hits = []
        for record in self.records.values():
            if record.scope != scope:
                continue
            terms = set(record.text.lower().split())
            score = len(query_terms & terms) / max(1, len(query_terms))
            if score >= min_score:
                hits.append(MemoryHit(
                    id=record.id,
                    text=record.text,
                    score=score,
                    scope=record.scope,
                    source_type=record.source_type,
                ))
        return sorted(hits, key=lambda hit: (-hit.score, hit.id))[:top_k]


class DeterministicRagIndex:
    def __init__(self):
        self.documents = [
            ("sql", "Use parameterized SQL queries to prevent injection."),
            ("xss", "Escape untrusted HTML output to prevent XSS."),
            ("auth", "Check authorization at the protected resource boundary."),
        ]

    def search(self, query, *, top_k=5, **kwargs):
        terms = set(query.lower().split())
        ranked = [
            {"id": item_id, "text": text, "score": len(terms & set(text.lower().split()))}
            for item_id, text in self.documents
        ]
        return sorted(ranked, key=lambda item: (-item["score"], item["id"]))[:top_k]


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * ratio)))
    return ordered[index]


def measure(name: str, iterations: int, function, *, note: str = "") -> dict:
    for _ in range(min(5, iterations)):
        function()
    samples = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        function()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return {
        "scenario": name,
        "iterations": iterations,
        "median_ms": round(statistics.median(samples), 6),
        "p95_ms": round(percentile(samples, 0.95), 6),
        "note": note,
    }


def pipeline_for(responses):
    provider = ScriptedModel(responses)
    budgeter = ContextBudgeter.from_env()
    registry = registry_with_tool(
        "example_tool",
        RecordingTool(output="ok"),
        modes={"bot"},
    )
    pipeline = Runtime(
        tools=registry,
        provider=provider,
        model="fake-model",
        tool_executor=ToolExecutor([]),
        context_builder=ContextBuilder(
            budgeter=budgeter,
            prompt_assets_service=PromptAssetsService(
                budgeter=budgeter,
                instruction_root=ROOT,
                skill_loader=SimpleNamespace(catalog_text=lambda: ""),
            ),
        ),
        memory_lifecycle=None,
        max_tokens=256,
        max_reasoning_steps=8,
    )
    return pipeline, provider


def run_pipeline(responses):
    pipeline, _ = pipeline_for(responses)
    session = Session(id="bench:chat", active_agent="bot")
    session.add_message("user", "hello")
    pipeline.run(AgentSpec.from_profile(PROFILE), "hello", RunContext(session=session))


def run_runtime_facade():
    pipeline, _ = pipeline_for([FinalResponse("ok")])
    session = Session(id="bench:runtime-facade", active_agent="bot")
    session.add_message("user", "hello")
    pipeline.run(
        AgentSpec.from_profile(PROFILE),
        "hello",
        RunContext(session=session),
    )


def run_streaming_pipeline():
    pipeline, provider = pipeline_for([FinalResponse("streamed")])
    provider.stream_chunks = ["stream", "ed"]
    session = Session(id="bench:stream", active_agent="bot")
    session.add_message("user", "hello")
    pipeline.run(
        AgentSpec.from_profile(PROFILE),
        "hello",
        RunContext(session=session, on_text=lambda chunk: None),
    )


def run_cancelled_pipeline():
    pipeline, _ = pipeline_for([FinalResponse("unused")])
    session = Session(id="bench:cancel", active_agent="bot")
    session.add_message("user", "cancel")
    pipeline.run(
        AgentSpec.from_profile(PROFILE),
        "cancel",
        RunContext(session=session, cancel_requested=lambda: True),
    )


def run_traced_pipeline():
    pipeline, _ = pipeline_for([FinalResponse("ok")])
    session = Session(id="bench:trace", active_agent="bot")
    session.add_message("user", "hello")
    pipeline.run(
        AgentSpec.from_profile(PROFILE),
        "hello",
        RunContext(session=session, run_state=__import__(
            "runtime.trace.run_state",
            fromlist=["RunState"],
        ).RunState.create(session_id=session.id),
        trace_store=NullTrace()),
    )


def run_disk_traced_pipeline():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline, _ = pipeline_for([FinalResponse("ok")])
        session = Session(id="bench:disk-trace", active_agent="bot")
        session.add_message("user", "hello")
        run_state = RunState.create(session_id=session.id)
        previous = os.environ.get("TRACE_INDEX_ENABLED")
        os.environ["TRACE_INDEX_ENABLED"] = "0"
        try:
            trace = TraceStore(Path(tmp) / "runs")
        finally:
            if previous is None:
                os.environ.pop("TRACE_INDEX_ENABLED", None)
            else:
                os.environ["TRACE_INDEX_ENABLED"] = previous
        trace.start_run(run_state)
        reply = pipeline.run(
            AgentSpec.from_profile(PROFILE),
            "hello",
            RunContext(session=session, run_state=run_state, trace_store=trace),
        ).output
        run_state.finish_success(reply)
        trace.write_run_state(run_state)
        trace.write_report(run_state, {"benchmark": True})


def vector_memory_search():
    index = DeterministicVectorIndex()
    for number, text in enumerate((
        "python pytest fixtures",
        "postgres session persistence",
        "runtime trace events",
        "coding workspace isolation",
    )):
        index.upsert(MemoryRecord(
            id=str(number),
            text=text,
            scope="bench",
            source_type="fixture",
        ))
    return index.search(
        query="runtime trace",
        scope="bench",
        top_k=3,
        min_score=0.0,
    )


def rag_search():
    return DeterministicRagIndex().search("prevent sql injection", top_k=3)


def coding_application_create():
    with tempfile.TemporaryDirectory() as tmp:
        sessions = InMemorySessions()
        return TaskSessionFactory(sessions, root=Path(tmp) / "tasks").create(
            parent_session_id="bench:parent",
            task_type="coding",
            user_request="benchmark",
            user_id="bench",
            user_role="admin",
        ).session.metadata["kind"]


def subagent_run():
    answer = json.dumps({
        "schema_version": "subagent.explore.v1",
        "agent_type": "explore",
        "status": "completed",
        "summary": "done",
        "payload": {
            "findings": [],
            "evidence": [],
            "covered_scope": [],
            "open_questions": [],
            "needs_parent_verification": False,
        },
        "incomplete": False,
    })
    pipeline, _ = pipeline_for([FinalResponse(answer)])
    TaskSubagentRunner(base_pipeline=pipeline, max_reasoning_steps=4).run(
        prompt="inspect",
        agent_type="explore",
        parent_session=Session(
            id="bench:parent",
            metadata={"user_role": "admin", "workspace_root": str(ROOT)},
        ),
    )


def context_sample(profile=PROFILE):
    session = Session(id="bench:context", active_agent=profile.tool_mode)
    session.add_message("user", "measure deterministic context")
    budgeter = ContextBudgeter.from_env()
    builder = ContextBuilder(
        budgeter=budgeter,
        context_providers=DEFAULT_CONTEXT_PROVIDERS,
        prompt_assets_service=PromptAssetsService(
            budgeter=budgeter,
            instruction_root=ROOT,
            skill_loader=SimpleNamespace(catalog_text=lambda: ""),
        ),
        coding_context_view_builder=build_coding_context_view,
    )
    return builder.build(session=session, profile=profile)


def benchmarks(iterations: int) -> tuple[list[dict], dict]:
    executor = ToolExecutor([])
    tool = RecordingTool(output="ok")
    scenarios = [
        measure(
            "pipeline_construction",
            iterations,
            lambda: pipeline_for([FinalResponse("ok")]),
            note="Current Runtime + AgentRunner fixture; excludes model pool and external stores.",
        ),
        measure(
            "chat_no_tool_runtime",
            iterations,
            lambda: run_pipeline([FinalResponse("ok")]),
            note="Fake model; includes context construction and model-external runtime.",
        ),
        measure(
            "runtime_facade_no_tool",
            iterations,
            run_runtime_facade,
            note="Runtime.run + RunContext + RunResult over the existing no-tool Runtime.",
        ),
        measure(
            "chat_one_tool_runtime",
            iterations,
            lambda: run_pipeline([
                ToolResponse("example_tool", {"value": 1}),
                FinalResponse("done"),
            ]),
            note="One fake tool call followed by final response.",
        ),
        measure(
            "chat_three_tool_runtime",
            iterations,
            lambda: run_pipeline([
                ToolResponse("example_tool", {"value": 1}, "one"),
                ToolResponse("example_tool", {"value": 2}, "two"),
                ToolResponse("example_tool", {"value": 3}, "three"),
                FinalResponse("done"),
            ]),
            note="Three sequential fake tool calls.",
        ),
        measure(
            "tool_executor_wrapper",
            iterations,
            lambda: executor.execute(
                ToolExecutionRequest(
                    call_id="bench",
                    tool_name="example_tool",
                    arguments={"value": 1},
                    session_id="bench",
                ),
                lambda name, arguments: tool(**arguments),
            ),
            note="ToolExecutor only; no shell, network, or persistence.",
        ),
        measure(
            "chat_context_build",
            iterations,
            lambda: context_sample(PROFILE),
        ),
        measure(
            "coding_context_build",
            iterations,
            lambda: context_sample(make_agent_spec("coding", "coding prompt", "coding")),
        ),
        measure(
            "chat_context_build_with_memory",
            iterations,
            lambda: ContextBuilder(
                context_providers=DEFAULT_CONTEXT_PROVIDERS,
                memory_service=ContextMemoryService(
                    memory_store=MemoryStore("Use deterministic fixtures."),
                ),
                prompt_assets_service=PromptAssetsService(
                    budgeter=ContextBudgeter.from_env(),
                    instruction_root=ROOT,
                    skill_loader=SimpleNamespace(catalog_text=lambda: ""),
                ),
            ).build(
                session=Session(
                    id="bench:memory",
                    messages=[{"role": "user", "content": "hello"}],
                ),
                profile=PROFILE,
            ),
            note="Local in-memory text only; no vector retrieval.",
        ),
        measure(
            "chat_trace_enabled",
            iterations,
            run_traced_pipeline,
            note="Null trace sink; measures event construction/callback overhead, not disk I/O.",
        ),
        measure(
            "chat_trace_disk_write",
            iterations,
            run_disk_traced_pipeline,
            note="Real TraceStore JSONL/report writes with index disabled; includes temporary directory lifecycle.",
        ),
        measure(
            "chat_streaming",
            iterations,
            run_streaming_pipeline,
            note="Two deterministic text chunks and no transport I/O.",
        ),
        measure(
            "cancellation_before_model",
            iterations,
            run_cancelled_pipeline,
        ),
        measure(
            "coding_application_factory",
            iterations,
            coding_application_create,
            note="In-memory Session manager; includes temporary directory setup and cleanup.",
        ),
        measure(
            "subagent_create_and_return",
            iterations,
            subagent_run,
            note="Independent Session and filtered tools with one fake final model response.",
        ),
        measure(
            "vector_memory_search",
            iterations,
            vector_memory_search,
            note="Deterministic in-process MemoryVectorIndex test double; four records, no Qdrant/network.",
        ),
        measure(
            "security_rag_search",
            iterations,
            rag_search,
            note="Deterministic local RAG index test double; three documents, no embedding model/network.",
        ),
    ]
    chat_context = context_sample(PROFILE)
    coding_context = context_sample(make_agent_spec("coding", "coding prompt", "coding"))
    metadata = {
        "python": __import__("platform").python_version(),
        "platform": __import__("platform").platform(),
        "timer": "perf_counter_ns",
        "network": False,
        "tokens": {
            "chat_estimated": estimate_tokens(chat_context.messages),
            "coding_estimated": estimate_tokens(coding_context.messages),
        },
        "component_counts": {
            "chat_candidate_chain": 9,
            "coding_candidate_chain": 13,
        },
        "limitations": [
            "Full build_runtime is excluded because it resolves configured providers, stores, and optional RAG.",
            "Task-session timing covers TaskSessionFactory; full Coding lifecycle is covered by the closure test.",
            "Disk trace timing includes local filesystem and temporary directory overhead.",
            "Vector Memory/RAG timings isolate runtime integration contracts with deterministic local indexes; they are not Qdrant/model throughput measurements.",
        ],
    }
    return scenarios, metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    scenarios, metadata = benchmarks(max(5, args.iterations))
    payload = {"metadata": metadata, "results": scenarios}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
