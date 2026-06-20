import tempfile
import unittest
from pathlib import Path

from sessions.session import Session
from tools import handlers
from tools.tool_registry import build_lead_tool_registry, build_teammate_tool_registry


def _session_for(root: Path, *, mode: str = "coding", kind: str = "task_session") -> Session:
    return Session(
        id=f"{kind}:grep-nl",
        current_mode=mode,
        metadata={
            "kind": kind,
            "workspace_root": str(root),
            "user_role": "admin",
        },
    )


class GrepNlToolTests(unittest.TestCase):
    def test_nl_reads_file_with_line_numbers_and_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "notes.txt").write_text("alpha\n\nbeta\ngamma\n", encoding="utf-8")
            session = _session_for(workspace)

            output = handlers.run_nl("notes.txt", offset=1, limit=2, _session=session)

        self.assertIn("     2\t", output)
        self.assertIn("     3\tbeta", output)
        self.assertIn("offset=3", output)

    def test_grep_searches_workspace_with_literal_and_glob(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "src").mkdir()
            (workspace / "src" / "app.py").write_text("needle = 1\n", encoding="utf-8")
            (workspace / "src" / "app.md").write_text("needle in docs\n", encoding="utf-8")
            session = _session_for(workspace)

            output = handlers.run_grep(
                "needle",
                path="src",
                glob="*.py",
                literal=True,
                _session=session,
            )

        self.assertIn("src/app.py:1:needle = 1", output)
        self.assertNotIn("app.md", output)

    def test_grep_and_nl_reject_paths_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = _session_for(workspace)

            grep_output = handlers.run_grep("needle", path="../outside", _session=session)
            nl_output = handlers.run_nl("../outside.txt", _session=session)

        self.assertIn("Path escapes workspace", grep_output)
        self.assertIn("Path escapes workspace", nl_output)

    def test_grep_and_nl_are_visible_for_coding_and_teammate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            coding = _session_for(workspace, mode="coding", kind="task_session")
            teammate = _session_for(workspace, mode="teammate", kind="teammate")

            lead_registry = build_lead_tool_registry()
            teammate_registry = build_teammate_tool_registry("alice")

            self.assertIn("grep", lead_registry.visible_names_for_turn(coding, "coding"))
            self.assertIn("nl", lead_registry.visible_names_for_turn(coding, "coding"))
            self.assertIn("grep", teammate_registry.visible_names_for_turn(teammate, "teammate"))
            self.assertIn("nl", teammate_registry.visible_names_for_turn(teammate, "teammate"))

    def test_registry_execution_uses_session_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "target.txt").write_text("needle\nsecond\n", encoding="utf-8")
            session = _session_for(workspace)
            registry = build_lead_tool_registry()

            grep_output = registry.execute(
                "grep",
                {"pattern": "needle", "path": ".", "literal": True},
                session=session,
                mode="coding",
            )
            nl_output = registry.execute(
                "nl",
                {"path": "target.txt", "limit": 1},
                session=session,
                mode="coding",
            )

        self.assertIn("target.txt:1:needle", grep_output)
        self.assertIn("     1\tneedle", nl_output)


if __name__ == "__main__":
    unittest.main()
