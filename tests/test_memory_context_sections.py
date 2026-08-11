from datetime import datetime, timezone

from memory.command_service import MemoryCommandService
from memory.commands import MemoryContext, MemoryWriteProposal
from memory.domain import MemoryKind, MemoryOwnerScope, MemorySourceType
from memory.index_sync import MemoryIndexSynchronizer
from memory.semantic_retrieval import SemanticMemoryRetrievalService
from runtime.context.memory import ContextMemoryService
from runtime.context.providers import MemoryContextProvider
from runtime.context.budget import ContextBudgeter, SectionBudgetRule
from runtime.sessions.session import Session
from tests.fakes.in_memory_memory_index import InMemoryMemoryIndex
from tests.fakes.in_memory_memory_repository import InMemoryMemoryRepository


NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)


def semantic_fixture():
    repository = InMemoryMemoryRepository()
    index = InMemoryMemoryIndex()
    commands = MemoryCommandService(repository, clock=lambda: NOW)
    context = MemoryContext(user_id="alice", session_id="web:alice:a")
    commands.remember(MemoryWriteProposal(
        content="Prefer concise answers",
        kind=MemoryKind.PREFERENCE,
        owner_scope=MemoryOwnerScope.USER,
        owner_id="alice",
        source_type=MemorySourceType.EXPLICIT_USER,
        explicit_user_request=True,
        confidence=1.0,
        salience=0.8,
    ), context)
    MemoryIndexSynchronizer(repository, index, clock=lambda: NOW).drain()
    retrieval = SemanticMemoryRetrievalService(
        repository,
        index,
        clock=lambda: NOW,
    )
    return repository, retrieval


def test_semantic_context_has_no_working_memory_channel() -> None:
    _, retrieval = semantic_fixture()
    service = ContextMemoryService(
        semantic_memory_retriever=retrieval,
    )
    session = Session(id="web:alice:a", metadata={"user_id": "alice"})

    semantic = service.build_memory_block(session, current_request="concise")
    assert semantic.startswith("<semantic_memory>")
    assert "Prefer concise answers" in semantic
    assert "working_memory" not in semantic


def test_semantic_provider_uses_independent_budget() -> None:
    _, retrieval = semantic_fixture()
    service = ContextMemoryService(semantic_memory_retriever=retrieval)
    budgeter = ContextBudgeter(
        enabled=True,
        rules={
            "semantic_memory": SectionBudgetRule(
                name="semantic_memory",
                budget_chars=80,
                floor_chars=20,
                strategy="head_tail",
            ),
        },
    )

    class Builder:
        pass

    builder = Builder()
    builder.memory_service = service
    builder.budgeter = budgeter

    rendered = MemoryContextProvider().provide(
        builder,
        session=Session(id="web:alice:a", metadata={"user_id": "alice"}),
        agent_spec=object(),
        current_request="concise",
    )

    assert rendered.budgeted_memory.name == "semantic_memory"
    assert rendered.budgeted_memory.truncated


def test_new_session_can_read_user_semantic_memory() -> None:
    _, retrieval = semantic_fixture()
    service = ContextMemoryService(semantic_memory_retriever=retrieval)

    block = service.build_memory_block(
        Session(id="web:alice:b", metadata={"user_id": "alice"}),
        current_request="concise",
    )

    assert "Prefer concise answers" in block
