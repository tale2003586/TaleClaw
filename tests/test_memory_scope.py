import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory.store import MemoryStore
from sessions.session import Session
from agents.coding import session as task_session_module
from agents.coding.session import TaskSessionFactory
from tools import handlers
from tools.tool_registry import ToolRegistry


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
    registry.register(
        _tool_schema("memorize"),
        handlers.MEMORY_HANDLERS["memorize"],
        enabled_modes={"bot", "coding"},
    )
    registry.register(
        _tool_schema("recall_memory"),
        handlers.MEMORY_HANDLERS["recall_memory"],
        enabled_modes={"bot", "coding"},
    )
    return registry


class MemoryScopeTests(unittest.TestCase):
    def test_task_session_factory_stores_portable_relative_memory_root(self) -> None:
        class RecordingSessions:
            def get_or_create(self, session_id: str) -> Session:
                return Session(id=session_id)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(task_session_module, "WORKDIR", root):
                record = TaskSessionFactory(RecordingSessions()).create(
                    parent_session_id="web:default",
                    task_type="coding",
                    user_request="fix the bug",
                )

            self.assertEqual(
                f".task_sessions/{record.task_id}/memory",
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

    def test_task_session_memorize_and_recall_use_local_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            global_memory = MemoryStore(root / "memory")
            task_memory_root = root / ".task_sessions"
            local_memory_root = task_memory_root / "coding-12345678" / "memory"
            session = Session(
                id="task:coding-12345678",
                current_mode="coding",
                metadata={
                    "kind": "task_session",
                    "task_id": "coding-12345678",
                    "memory_root": ".task_sessions/coding-12345678/memory",
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

    def test_task_session_memory_root_cannot_escape_task_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            global_memory = MemoryStore(root / "memory")
            task_memory_root = root / ".task_sessions"
            session = Session(
                id="task:coding-12345678",
                current_mode="coding",
                metadata={
                    "kind": "task_session",
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

            self.assertEqual("Error: Task memory root escapes .task_sessions.", result)
            self.assertFalse((root / "outside").exists())


if __name__ == "__main__":
    unittest.main()
