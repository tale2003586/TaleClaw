import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.sessions.session import Session
from tools import handlers
from tools.tool_registry import build_lead_tool_registry


class BotStorageToolTests(unittest.TestCase):
    def test_bot_mode_defers_scoped_storage_tools(self) -> None:
        registry = build_lead_tool_registry()
        session = Session(id="web:default", active_agent="bot")

        visible = registry.visible_names_for_turn(session, "bot")

        self.assertNotIn("storage_list_files", visible)
        self.assertNotIn("storage_read_file", visible)
        self.assertNotIn("storage_write_file", visible)
        self.assertNotIn("write_file", visible)
        self.assertNotIn("edit_file", visible)
        self.assertNotIn("bash", visible)

    def test_bot_write_creates_generated_artifact_and_audit_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            registry = build_lead_tool_registry()
            session = Session(id="web:report-chat", active_agent="bot")

            with patch.object(handlers, "WORKDIR", workspace):
                registry.execute(
                    "tool_search",
                    {"query": "select:storage_write_file"},
                    session=session,
                    mode="bot",
                )
                result = registry.execute(
                    "storage_write_file",
                    {
                        "path": "reports/ai-daily.md",
                        "content": "# AI Daily\n\nUseful findings.",
                    },
                    session=session,
                    mode="bot",
                )

            payload = json.loads(result)
            artifact = workspace / "storage" / "generated" / "reports" / "ai-daily.md"
            records = workspace / "storage" / "records" / "storage_writes.jsonl"
            audit = json.loads(records.read_text(encoding="utf-8").strip())

            self.assertEqual("created", payload["status"])
            self.assertEqual("generated/reports/ai-daily.md", payload["path"])
            self.assertEqual("# AI Daily\n\nUseful findings.", artifact.read_text(encoding="utf-8"))
            self.assertEqual("web:report-chat", audit["session_id"])
            self.assertEqual(payload["path"], audit["path"])
            self.assertEqual(payload["bytes"], audit["bytes"])
            self.assertEqual(64, len(audit["sha256"]))

    def test_write_rejects_escape_and_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            with patch.object(handlers, "WORKDIR", workspace):
                escape = handlers.run_storage_write("../outside.txt", "no")
                created = handlers.run_storage_write("notes/existing.txt", "first")
                overwrite = handlers.run_storage_write("notes/existing.txt", "second")

            self.assertIn("escapes allowed directory", escape)
            self.assertEqual("created", json.loads(created)["status"])
            self.assertIn("already exists", overwrite)
            self.assertFalse((workspace / "storage" / "outside.txt").exists())
            self.assertEqual(
                "first",
                (workspace / "storage" / "generated" / "notes" / "existing.txt").read_text(),
            )

    def test_write_rejects_large_or_unsupported_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            with (
                patch.object(handlers, "WORKDIR", workspace),
                patch.object(handlers, "MAX_STORAGE_WRITE_BYTES", 4),
            ):
                too_large = handlers.run_storage_write("notes/large.txt", "12345")
                unsupported = handlers.run_storage_write("notes/archive.zip", "1234")

            self.assertIn("too large", too_large)
            self.assertIn("Unsupported storage artifact type", unsupported)

    def test_list_and_read_can_access_storage_but_not_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            uploads = workspace / "storage" / "uploads"
            uploads.mkdir(parents=True)
            (uploads / "note.txt").write_text("first\nsecond\nthird", encoding="utf-8")
            (workspace / "private.txt").write_text("secret", encoding="utf-8")

            with patch.object(handlers, "WORKDIR", workspace):
                listing = json.loads(handlers.run_storage_list("uploads"))
                read = handlers.run_storage_read("uploads/note.txt", limit=2)
                escape = handlers.run_storage_read("../private.txt")

            self.assertEqual("uploads/note.txt", listing["entries"][0]["path"])
            self.assertIn("first\nsecond", read)
            self.assertIn("storage_read_file", read)
            self.assertIn("offset=2", read)
            self.assertIn("1 lines remain", read)
            self.assertIn("escapes allowed directory", escape)


if __name__ == "__main__":
    unittest.main()
