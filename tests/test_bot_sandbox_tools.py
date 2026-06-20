import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sessions.session import Session
from tools import handlers
from tools.tool_registry import build_lead_tool_registry


class BotSandboxToolTests(unittest.TestCase):
    def test_bot_mode_sees_sandbox_tools_but_not_workspace_write_tools(self) -> None:
        registry = build_lead_tool_registry()
        session = Session(id="web:default", current_mode="bot")

        visible = registry.visible_names_for_turn(session, "bot")

        self.assertIn("sandbox_list_files", visible)
        self.assertIn("sandbox_read_file", visible)
        self.assertIn("sandbox_write_file", visible)
        self.assertIn("publish_artifact", visible)
        self.assertNotIn("write_file", visible)
        self.assertNotIn("edit_file", visible)
        self.assertNotIn("bash", visible)

    def test_regular_sessions_receive_isolated_sandbox_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first = Session(id="web:first", current_mode="bot")
            second = Session(id="web:second", current_mode="bot")

            with patch.object(handlers, "WORKDIR", workspace):
                first_result = handlers.run_sandbox_write("draft.md", "first", _session=first)
                second_result = handlers.run_sandbox_write("draft.md", "second", _session=second)
                first_scope = handlers._sandbox_scope_root(first)
                second_scope = handlers._sandbox_scope_root(second)

            self.assertEqual("created", json.loads(first_result)["status"])
            self.assertEqual("created", json.loads(second_result)["status"])
            self.assertNotEqual(first_scope, second_scope)
            self.assertEqual("first", (first_scope / "draft.md").read_text(encoding="utf-8"))
            self.assertEqual("second", (second_scope / "draft.md").read_text(encoding="utf-8"))

    def test_task_session_uses_readable_task_id_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = Session(
                id="task:coding-12345678",
                current_mode="coding",
                metadata={
                    "kind": "task_session",
                    "task_id": "coding-12345678",
                },
            )

            with patch.object(handlers, "WORKDIR", workspace):
                result = handlers.run_sandbox_write("notes/draft.md", "draft", _session=session)
                scope = handlers._sandbox_scope_root(session)

            self.assertEqual("created", json.loads(result)["status"])
            self.assertEqual(
                workspace / ".task_sandbox" / "tasks" / "coding-12345678",
                scope,
            )
            self.assertEqual("draft", (scope / "notes" / "draft.md").read_text())

    def test_sandbox_write_requires_explicit_overwrite_and_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = Session(id="web:drafts", current_mode="bot")

            with patch.object(handlers, "WORKDIR", workspace):
                created = handlers.run_sandbox_write("draft.md", "one", _session=session)
                blocked = handlers.run_sandbox_write("draft.md", "two", _session=session)
                updated = handlers.run_sandbox_write(
                    "draft.md",
                    "two",
                    overwrite=True,
                    _session=session,
                )
                escape = handlers.run_sandbox_write("../outside.md", "no", _session=session)
                scope = handlers._sandbox_scope_root(session)

            self.assertEqual("created", json.loads(created)["status"])
            self.assertIn("overwrite=true", blocked)
            self.assertEqual("updated", json.loads(updated)["status"])
            self.assertIn("escapes allowed directory", escape)
            self.assertEqual("two", (scope / "draft.md").read_text())
            self.assertFalse((workspace / ".task_sandbox" / "outside.md").exists())

    def test_publish_copies_final_artifact_and_records_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = Session(id="web:publish", current_mode="bot")

            with patch.object(handlers, "WORKDIR", workspace):
                handlers.run_sandbox_write("drafts/report.md", "# final", _session=session)
                published = handlers.run_publish_artifact(
                    "drafts/report.md",
                    "reports/final.md",
                    _session=session,
                )
                duplicate = handlers.run_publish_artifact(
                    "drafts/report.md",
                    "reports/final.md",
                    _session=session,
                )

            payload = json.loads(published)
            target = workspace / "storage" / "generated" / "reports" / "final.md"
            record_path = workspace / "storage" / "records" / "storage_writes.jsonl"
            record = json.loads(record_path.read_text(encoding="utf-8").strip())

            self.assertEqual("published", payload["status"])
            self.assertEqual("generated/reports/final.md", payload["path"])
            self.assertEqual("# final", target.read_text(encoding="utf-8"))
            self.assertIn("already exists", duplicate)
            self.assertEqual("publish_artifact", record["operation"])
            self.assertEqual("drafts/report.md", record["source_path"])
            self.assertEqual("web:publish", record["session_id"])

    def test_generated_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            workspace = Path(tmp)
            generated = workspace / "storage" / "generated"
            generated.parent.mkdir(parents=True)
            generated.symlink_to(Path(outside), target_is_directory=True)

            with patch.object(handlers, "WORKDIR", workspace):
                result = handlers.run_storage_write("report.md", "no")

            self.assertIn("Generated storage directory escapes storage", result)
            self.assertFalse((Path(outside) / "report.md").exists())

    def test_cleanup_removes_only_expired_session_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            old_scope = workspace / ".task_sandbox" / "sessions" / "old"
            active_scope = workspace / ".task_sandbox" / "sessions" / "active"
            old_scope.mkdir(parents=True)
            active_scope.mkdir(parents=True)
            os.utime(old_scope, (100.0, 100.0))
            os.utime(active_scope, (900.0, 900.0))

            with patch.object(handlers, "WORKDIR", workspace):
                removed = handlers.cleanup_expired_sandboxes(
                    max_age_seconds=500.0,
                    now=1000.0,
                )

            self.assertEqual(1, removed)
            self.assertFalse(old_scope.exists())
            self.assertTrue(active_scope.exists())


if __name__ == "__main__":
    unittest.main()
