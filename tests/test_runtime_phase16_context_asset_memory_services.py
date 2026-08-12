from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

from runtime.context import (
    ContextBuilder,
    ContextMemoryService,
    PromptAssetsService,
)
from runtime.context.budget import ContextBudgeter
from runtime.context.providers import DEFAULT_CONTEXT_PROVIDERS
from runtime.sessions.session import Session


class _MemoryRetriever:
    def retrieve(self, request, context):
        return f"memory for {request}"

    def render(self, result):
        return f"<memory>\n{result}\n</memory>"

    def drain_trace_events(self):
        return []


def test_context_builder_delegates_prompt_assets_and_memory_rendering():
    source = inspect.getsource(ContextBuilder)

    assert "_read_instruction_file" not in source
    assert "_instruction_files" not in source
    assert "_skill_catalog_signature" not in source
    assert "read_all()" not in source
    assert ".recall(" not in source


def test_explicit_prompt_assets_service_preserves_instruction_rendering(
    tmp_path: Path,
):
    (tmp_path / "AGENTS.md").write_text("service instruction", encoding="utf-8")
    budgeter = ContextBudgeter.from_env()
    service = PromptAssetsService(
        budgeter=budgeter,
        instruction_root=tmp_path,
    )
    builder = ContextBuilder(
        budgeter=budgeter,
        prompt_assets_service=service,
    )

    context = builder.build(
        session=Session(id="phase16:assets"),
        agent_spec=SimpleNamespace(instructions="base", tool_mode="coding"),
    )

    assert "service instruction" in context.messages[0]["content"]


def test_explicit_memory_service_preserves_durable_memory_only():
    service = ContextMemoryService(
        semantic_memory_retriever=_MemoryRetriever(),
    )
    builder = ContextBuilder(
        context_providers=DEFAULT_CONTEXT_PROVIDERS,
        memory_service=service,
    )
    session = Session(id="phase16:memory")
    session.add_message("user", "current request")

    context = builder.build(
        session=session,
        agent_spec=SimpleNamespace(instructions="base", tool_mode="bot"),
    )

    rendered = "\n".join(str(message.get("content", "")) for message in context.messages)
    assert "<memory>\nmemory for current request\n</memory>" in rendered
