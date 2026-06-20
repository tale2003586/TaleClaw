import tempfile
import unittest
from pathlib import Path

from memory.store import MemoryStore


class MemoryRecallTests(unittest.TestCase):
    def test_query_recall_returns_relevant_snippets_not_full_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "memory")
            memory.append("memory", "项目测试优先使用 pytest 和 fixture。")
            memory.append("memory", "用户喜欢真实人物细节。")
            memory.append_history("USER:\n讨论 PostgreSQL 连接池。\n\nASSISTANT_SUMMARY:\n建议使用 psycopg pool。")

            result = memory.recall("pytest 测试")

            self.assertIn("pytest", result)
            self.assertIn("memory_hit", result)
            self.assertNotIn("真实人物细节", result)
            self.assertNotIn("PostgreSQL 连接池", result)

    def test_recall_without_query_keeps_prompt_memory_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "memory")
            memory.append("memory", "stable fact")

            result = memory.recall()

            self.assertIn("<memory>", result)
            self.assertIn("stable fact", result)


if __name__ == "__main__":
    unittest.main()
