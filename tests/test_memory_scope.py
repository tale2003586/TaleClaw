import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory.store import MemoryStore
from runtime.sessions.session import Session
from applications.coding import session as coding_application_module
from applications.coding.session import TaskSessionFactory
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


def _memory_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolSpec(
        schema=_tool_schema("memorize"),
        handler=handlers.MEMORY_HANDLERS["memorize"],
        allowed_modes=frozenset({"bot", "coding"}),
        session_scoped=True,
    ))
    registry.register(ToolSpec(
        schema=_tool_schema("recall_memory"),
        handler=handlers.MEMORY_HANDLERS["recall_memory"],
        allowed_modes=frozenset({"bot", "coding"}),
        session_scoped=True,
    ))
    return registry


class MemoryScopeTests(unittest.TestCase):
    def tearDown(self) -> None:
        handlers.configure_semantic_memory_services()

    def test_coding_application_factory_stores_portable_relative_memory_root(self) -> None:
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

            self.assertEqual(
                f".coding_applications/{record.task_id}/memory",
                record.session.metadata["memory_root"],
            )

    def test_regular_session_memorize_writes_global_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            global_memory = MemoryStore(Path(tmp) / "memory")
            registry = _memory_registry()
            session = Session(id="web:default")

            with patch.object(handlers, "MEMORY", global_memory):
                result = registry.execute(
                    "memorize",
                    {"content": "global preference"},
                    session=session,
                    mode="bot",
                )

            self.assertEqual("Saved to MEMORY.md", result)
            self.assertIn("global preference", global_memory.memory_path.read_text())

    def test_semantic_memorize_uses_repository_not_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            global_memory = MemoryStore(Path(tmp) / "memory")
            before = global_memory.memory_path.read_text()
            repository = InMemoryMemoryRepository()
            index = InMemoryMemoryIndex()
            commands = MemoryCommandService(repository)
            retrieval = SemanticMemoryRetrievalService(repository, index)
            synchronizer = MemoryIndexSynchronizer(repository, index)
            handlers.configure_semantic_memory_services(
                command_service=commands,
                retrieval_service=retrieval,
                index_synchronizer=synchronizer,
            )
            registry = _memory_registry()
            session = Session(
                id="web:alice:a",
                metadata={"user_id": "alice"},
            )

            with patch.object(handlers, "MEMORY", global_memory):
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
            self.assertEqual(before, global_memory.memory_path.read_text())
            self.assertEqual(1, len(repository.items))
            event_names = [
                event["event"]
                for event in session.metadata.get("memory_trace_events", [])
            ]
            self.assertIn("memory.item.created", event_names)
            self.assertIn("memory.index.completed", event_names)
            self.assertIn("memory.semantic.retrieved", event_names)

    def test_semantic_memorize_rejects_legacy_file_section(self) -> None:
        handlers.configure_semantic_memory_services(
            command_service=MemoryCommandService(InMemoryMemoryRepository()),
        )
        result = handlers.run_memorize(
            content="temporary state",
            section="now",
            _session=Session(id="web:default"),
        )

        self.assertIn("no longer accepts file sections", result)

    def test_coding_application_memorize_and_recall_use_local_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            global_memory = MemoryStore(root / "memory")
            task_memory_root = root / ".coding_applications"
            local_memory_root = task_memory_root / "coding-12345678" / "memory"
            session = Session(
                id="task:coding-12345678",
                active_agent="coding",
                metadata={
                    "kind": "coding_application",
                    "task_id": "coding-12345678",
                    "memory_root": ".coding_applications/coding-12345678/memory",
                },
            )
            registry = _memory_registry()
            global_memory.append("memory", "global-only fact")

            with (
                patch.object(handlers, "MEMORY", global_memory),
                patch.object(handlers, "WORKDIR", root),
                patch.object(handlers, "TASK_MEMORY_ROOT", task_memory_root.resolve()),
            ):
                save_result = registry.execute(
                    "memorize",
                    {"content": "task-only fact"},
                    session=session,
                    mode="coding",
                )
                recall_result = registry.execute(
                    "recall_memory",
                    {"query": "fact"},
                    session=session,
                    mode="coding",
                )

            self.assertEqual("Saved to MEMORY.md", save_result)
            self.assertNotIn("task-only fact", global_memory.memory_path.read_text())
            self.assertIn("task-only fact", (local_memory_root / "MEMORY.md").read_text())
            self.assertIn("task-only fact", recall_result)
            self.assertNotIn("global-only fact", recall_result)

    def test_coding_application_memory_root_cannot_escape_task_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            global_memory = MemoryStore(root / "memory")
            task_memory_root = root / ".coding_applications"
            session = Session(
                id="task:coding-12345678",
                active_agent="coding",
                metadata={
                    "kind": "coding_application",
                    "task_id": "coding-12345678",
                    "memory_root": str(root / "outside"),
                },
            )
            registry = _memory_registry()

            with (
                patch.object(handlers, "MEMORY", global_memory),
                patch.object(handlers, "TASK_MEMORY_ROOT", task_memory_root.resolve()),
            ):
                result = registry.execute(
                    "memorize",
                    {"content": "must not be written"},
                    session=session,
                    mode="coding",
                )

            self.assertEqual("Error: Task memory root escapes .coding_applications.", result)
            self.assertFalse((root / "outside").exists())


if __name__ == "__main__":
    unittest.main()
