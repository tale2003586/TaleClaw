import unittest

from memory.dedup import is_duplicate_memory, is_semantic_duplicate


class MemoryDedupTests(unittest.TestCase):
    def test_semantic_duplicate_catches_near_duplicate_pytest_memory(self) -> None:
        self.assertTrue(
            is_semantic_duplicate(
                "Use pytest for testing",
                "Use pytest to run tests",
            )
        )

    def test_duplicate_memory_uses_semantic_fallback(self) -> None:
        existing = "- Use pytest to run tests\n"

        self.assertTrue(is_duplicate_memory("Use pytest for testing", existing))


if __name__ == "__main__":
    unittest.main()
