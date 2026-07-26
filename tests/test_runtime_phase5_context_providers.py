from __future__ import annotations

from types import SimpleNamespace

from agents.definitions import BOT_AGENT_SPEC
from runtime.agent_spec import ContextPolicy
from runtime.context import ContextBuilder, ContextMemoryService, PromptAssetsService
from runtime.context.budget import ContextBudgeter
from runtime.context.providers import (
    CodingContextProvider,
    DEFAULT_CONTEXT_PROVIDERS,
    HistoryContextProvider,
    MemoryContextProvider,
    PromptContextProvider,
    RetrievalContextProvider,
)
from runtime.sessions import Session


class _Memory:
    def read_all(self):
        return "durable preference"


def _builder(**kwargs):
    memory_store = kwargs.pop("memory_store", None)
    budgeter = ContextBudgeter.from_env()
    return ContextBuilder(
        budgeter=budgeter,
        context_providers=DEFAULT_CONTEXT_PROVIDERS,
        prompt_assets_service=PromptAssetsService(
            budgeter=budgeter,
            skill_loader=SimpleNamespace(catalog_text=lambda: ""),
        ),
        memory_service=ContextMemoryService(memory_store=memory_store),
        **kwargs,
    )


def test_default_context_provider_order_is_explicit():
    builder = _builder()

    assert list(builder.context_providers) == [
        "prompt",
        "history",
        "memory",
        "retrieval",
        "coding",
    ]
    assert isinstance(builder.context_providers["prompt"], PromptContextProvider)
    assert isinstance(builder.context_providers["history"], HistoryContextProvider)
    assert isinstance(builder.context_providers["memory"], MemoryContextProvider)
    assert isinstance(builder.context_providers["retrieval"], RetrievalContextProvider)
    assert isinstance(builder.context_providers["coding"], CodingContextProvider)


def test_context_policy_can_exclude_history_and_memory_without_prompt_change():
    session = Session(id="phase5:policy")
    session.add_message("assistant", "old answer")
    session.add_message("user", "current request")
    builder = _builder(memory_store=_Memory())

    default = builder.build(session=session, profile=BOT_AGENT_SPEC)
    restricted = builder.build(
        session=session,
        profile=BOT_AGENT_SPEC,
        context_policy=ContextPolicy(
            name="restricted",
            include_history=False,
            include_memory=False,
        ),
    )

    assert default.messages[0] == restricted.messages[0]
    assert any("old answer" in str(message["content"]) for message in default.messages)
    assert not any(
        "old answer" in str(message["content"])
        for message in restricted.messages
    )
    assert any("<memory>" in str(message["content"]) for message in default.messages)
    assert not any(
        "<memory>" in str(message["content"])
        for message in restricted.messages
    )
    assert restricted.messages[-1]["content"] == "current request"
