import tempfile
from pathlib import Path
import unittest

from knowledge.chunking.base import KnowledgeChunk
from knowledge.incremental import IncrementalIngester


class FakeIndex:
    def __init__(self) -> None:
        self.deleted = []
        self.upserted = []

    def delete_file_chunks(self, source_path):
        self.deleted.append(str(source_path))

    def upsert_chunks(self, chunks, *, batch_size=64):
        self.upserted.append(list(chunks))
        return len(chunks)


def _iter_files(root: Path, **kwargs):
    return sorted(path for path in root.rglob("*.md") if path.is_file())


def _chunk_file(path: Path, *, root: Path, **kwargs):
    relpath = str(path.relative_to(root))
    return [
        KnowledgeChunk(
            id=f"chunk:{relpath}",
            text=path.read_text(),
            source_path=str(path),
            source_relpath=relpath,
            title=path.stem,
            chunk_index=0,
            char_start=0,
            char_end=path.stat().st_size,
            source_type="markdown",
            metadata={},
        )
    ]


class SecurityRagIncrementalTests(unittest.TestCase):
    def test_incremental_second_run_indexes_nothing_until_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            root.mkdir()
            state = Path(tmpdir) / "state.json"
            source = root / "a.md"
            source.write_text("first", encoding="utf-8")
            index = FakeIndex()
            ingester = IncrementalIngester(
                index=index,
                source_root=root,
                state_path=state,
                iter_files=_iter_files,
                chunk_file=_chunk_file,
            )

            first = ingester.sync()
            second = ingester.sync()
            source.write_text("second", encoding="utf-8")
            third = ingester.sync()

            self.assertEqual(1, first.files_indexed)
            self.assertEqual(0, second.files_indexed)
            self.assertEqual(1, third.files_indexed)
            self.assertEqual(2, len(index.upserted))
            self.assertTrue(state.exists())

    def test_incremental_deletes_removed_file_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            root.mkdir()
            state = Path(tmpdir) / "state.json"
            source = root / "a.md"
            source.write_text("first", encoding="utf-8")
            index = FakeIndex()
            ingester = IncrementalIngester(
                index=index,
                source_root=root,
                state_path=state,
                iter_files=_iter_files,
                chunk_file=_chunk_file,
            )

            ingester.sync()
            source.unlink()
            result = ingester.sync()

            self.assertEqual(1, result.files_deleted)
            self.assertIn(str(source), index.deleted)


if __name__ == "__main__":
    unittest.main()
