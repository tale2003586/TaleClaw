import json
import tempfile
import unittest
from pathlib import Path

from memory.candidates import CandidateMemoryStore


class CandidateStoreJsonTests(unittest.TestCase):
    def test_upsert_updates_json_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PENDING.json"
            store = CandidateMemoryStore(path)

            candidate, created = store.upsert(
                content="Use pytest for project tests",
                source_ref="web:test:1",
            )
            updated, created_again = store.upsert(
                content="Use pytest for project tests",
                source_ref="web:test:2",
            )

            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual(candidate.id, updated.id)
            self.assertEqual(2, updated.evidence_count)
            mirror = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("json", mirror["backend"])
            self.assertEqual(1, len(mirror["candidates"]))

    def test_existing_json_candidates_are_loaded_on_first_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PENDING.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "candidates": [
                            {
                                "id": "mem_cand_0007",
                                "content": "Prefer pytest",
                                "source_refs": ["old:1"],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            store = CandidateMemoryStore(path)
            candidates = store.read()

            self.assertEqual(1, len(candidates))
            self.assertEqual("mem_cand_0007", candidates[0].id)
            self.assertEqual("Prefer pytest", candidates[0].content)


if __name__ == "__main__":
    unittest.main()
