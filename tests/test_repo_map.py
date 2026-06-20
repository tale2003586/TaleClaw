import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sessions.session import Session
from tools import handlers
from tools.tool_registry import build_lead_tool_registry


class RepoMapToolTests(unittest.TestCase):
    def test_repo_map_returns_git_directory_aggregates_and_line_counts(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not available")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "src").mkdir()
            (workspace / "tests").mkdir()
            (workspace / "src" / "app.py").write_text("one\ntwo\n", encoding="utf-8")
            (workspace / "tests" / "test_app.py").write_text("alpha\nbeta", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
            session = Session(id="task:repo-map", metadata={"workspace_root": str(workspace)})

            payload = json.loads(handlers.run_repo_map(".", max_depth=2, _session=session))

            self.assertEqual(".", payload["path"])
            self.assertEqual("git", payload["source"])
            self.assertEqual(2, payload["total_files"])
            self.assertEqual(4, payload["total_lines"])
            directories = {item["path"]: item for item in payload["directories"]}
            self.assertEqual(2, directories["."]["file_count"])
            self.assertEqual(1, directories["src"]["file_count"])
            files = {item["path"]: item for item in payload["files"]}
            self.assertEqual(2, files["src/app.py"]["lines"])
            self.assertEqual(2, files["tests/test_app.py"]["lines"])

    def test_repo_map_paginates_and_caches_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            for index in range(12):
                (workspace / f"file_{index}.py").write_text("x\n", encoding="utf-8")
            session = Session(id="task:repo-map-cache", metadata={"workspace_root": str(workspace)})

            with patch.object(handlers, "REPO_MAP_MAX_CHARS", 400):
                first = handlers.run_repo_map(".", max_depth=1, _session=session)
                cached = handlers.run_repo_map(".", max_depth=1, _session=session)

            self.assertIn("To continue: repo_map(", first)
            self.assertIn("[tool-cache] already read", cached)

    def test_repo_map_is_visible_for_coding_lead(self) -> None:
        registry = build_lead_tool_registry()
        session = Session(id="task:repo-map-visible", current_mode="coding")

        visible = registry.visible_names_for_turn(session, "coding")

        self.assertIn("repo_map", visible)


if __name__ == "__main__":
    unittest.main()
