import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.sessions.session import Session
from applications.coding import session as coding_application_module
from applications.coding.session import TaskSessionFactory
from applications.coding.task_state import ensure_task_state
from tools import handlers
from tools.tool_registry import ToolRegistry
from tools.spec import ToolSpec
from memory.command_service import MemoryCommandService
from memory.index_sync import MemoryIndexSynchronizer
from memory.semantic_retrieval import SemanticMemoryRetrievalService
from tests.fakes.in_memory_memory_index import InMemoryMemoryIndex
from tests.fakes.in_memory_memory_repository import InMemoryMemoryRepository


def _tool_schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    }


def _memory_registry(**memory_services) -> ToolRegistry:
    memory_handlers = handlers.make_memory_handlers(**memory_services)
    registry = ToolRegistry()
    registry.register(ToolSpec(
        schema=_tool_schema("memorize"),
        handler=memory_handlers["memorize"],
        allowed_modes=frozenset({"bot", "coding"}),
        session_scoped=True,
    ))
    registry.register(ToolSpec(
        schema=_tool_schema("recall_memory"),
        handler=memory_handlers["recall_memory"],
        allowed_modes=frozenset({"bot", "coding"}),
        session_scoped=True,
    ))
    return registry


def _unlock_memory(registry: ToolRegistry, session: Session) -> None:
    mode = session.active_agent if session.active_agent in {"bot", "coding"} else "bot"
    for name in ("memorize", "recall_memory"):
        registry.execute(
            "tool_search",
            {"query": "save a long-term preference" if name == "memorize" else "recall a previous preference"},
            session=session,
            mode=mode,
        )


class MemoryScopeTests(unittest.TestCase):
    def test_coding_application_factory_does_not_create_memory_root(self) -> None:
        class RecordingSessions:
            def get_or_create(self, session_id: str) -> Session:
                return Session(id=session_id)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(coding_application_module, "WORKDIR", root):
                record = TaskSessionFactory(RecordingSessions()).create(
                    parent_session_id="web:default",
                    task_type="coding",
                    user_request="fix the bug",
                )

            self.assertTrue(record.task_root.is_dir())
            self.assertNotIn("memory_root", record.session.metadata)
            self.assertFalse((record.task_root / "memory").exists())

    def test_new_task_session_never_writes_retired_state_keys(self) -> None:
        class RecordingSessions:
            def get_or_create(self, session_id: str) -> Session:
                return Session(id=session_id)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(coding_application_module, "WORKDIR", root):
                record = TaskSessionFactory(RecordingSessions()).create(
                    parent_session_id="web:default",
                    task_type="coding",
                    user_request="inspect the runtime",
                )
            ensure_task_state(
                record.session,
                objective_summary="inspect the runtime",
            )

        self.assertTrue(
            {"working_memory", "coding_context_state", "memory_root"}.isdisjoint(
                record.session.metadata
            )
        )

    def test_disabled_memory_tool_does_not_create_legacy_markdown(self) -> None:
        registry = _memory_registry()
        session = Session(id="web:default")
        _unlock_memory(registry, session)

        result = registry.execute(
            "memorize",
            {"content": "global preference"},
            session=session,
            mode="bot",
        )

        self.assertEqual("Durable memory is not enabled.", result)

    def test_semantic_memorize_uses_repository_not_markdown(self) -> None:
        with tempfile.TemporaryDirectory():
            repository = InMemoryMemoryRepository()
            index = InMemoryMemoryIndex()
            commands = MemoryCommandService(repository)
            retrieval = SemanticMemoryRetrievalService(repository, index)
            synchronizer = MemoryIndexSynchronizer(repository, index)
            registry = _memory_registry(
                command_service=commands,
                retrieval_service=retrieval,
                index_synchronizer=synchronizer,
            )
            session = Session(
                id="web:alice:a",
                metadata={"user_id": "alice"},
            )
            _unlock_memory(registry, session)

            saved = registry.execute(
                "memorize",
                {"content": "Prefer concise answers"},
                session=session,
                mode="bot",
            )
            recalled = registry.execute(
                "recall_memory",
                {"query": "concise"},
                session=session,
                mode="bot",
            )

            self.assertIn("Saved semantic memory", saved)
            self.assertIn("<semantic_memory>", recalled)
            self.assertIn("Prefer concise answers", recalled)
            self.assertEqual(1, len(repository.items))
            event_names = [
                event["event"]
                for event in session.metadata.get("memory_trace_events", [])
            ]
            self.assertIn("memory.item.created", event_names)
            self.assertIn("memory.index.completed", event_names)
            self.assertIn("memory.semantic.retrieved", event_names)

    def test_memory_handler_dependencies_are_isolated_per_registry(self) -> None:
        first_repository = InMemoryMemoryRepository()
        second_repository = InMemoryMemoryRepository()
        first_registry = _memory_registry(
            command_service=MemoryCommandService(first_repository),
        )
        second_registry = _memory_registry(
            command_service=MemoryCommandService(second_repository),
        )
        first_session = Session(id="web:first", metadata={"user_id": "first"})
        second_session = Session(id="web:second", metadata={"user_id": "second"})
        _unlock_memory(first_registry, first_session)
        _unlock_memory(second_registry, second_session)

        first_registry.execute(
            "memorize",
            {"content": "first preference"},
            session=first_session,
            mode="bot",
        )
        second_registry.execute(
            "memorize",
            {"content": "second preference"},
            session=second_session,
            mode="bot",
        )

        self.assertEqual(["first preference"], [item.content for item in first_repository.items.values()])
        self.assertEqual(["second preference"], [item.content for item in second_repository.items.values()])

if __name__ == "__main__":
    unittest.main()
