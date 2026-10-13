import unittest

from memory.dedup import is_semantic_duplicate


class MemoryDedupTests(unittest.TestCase):
    def test_semantic_duplicate_catches_near_duplicate_pytest_memory(self) -> None:
        self.assertTrue(
            is_semantic_duplicate(
                "Use pytest for testing",
                "Use pytest to run tests",
            )
        )


if __name__ == "__main__":
    unittest.main()
