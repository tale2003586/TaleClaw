import tempfile
import unittest
from pathlib import Path

from sessions.session import Session
from tools import handlers
from tools.tool_registry import build_lead_tool_registry, build_teammate_tool_registry


def _session_for(root: Path, *, mode: str = "coding", kind: str = "task_session") -> Session:
    return Session(
        id=f"{kind}:rg",
        current_mode=mode,
        metadata={
            "kind": kind,
            "workspace_root": str(root),
            "user_role": "admin",
        },
    )


class RgToolTests(unittest.TestCase):
    def test_rg_searches_workspace_with_literal_and_glob(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "src").mkdir()
            (workspace / "src" / "app.py").write_text("needle = 1\n", encoding="utf-8")
            (workspace / "src" / "app.md").write_text("needle in docs\n", encoding="utf-8")
            session = _session_for(workspace)

            output = handlers.run_rg(
                "needle",
                path="src",
                glob="*.py",
                literal=True,
                _session=session,
            )

        self.assertIn("src/app.py:1:1:needle = 1", output)
        self.assertNotIn("app.md", output)

    def test_rg_rejects_paths_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = _session_for(workspace)

            output = handlers.run_rg("needle", path="../outside", _session=session)

        self.assertIn("Path escapes workspace", output)

    def test_rg_is_visible_for_coding_and_teammate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            coding = _session_for(workspace, mode="coding", kind="task_session")
            teammate = _session_for(workspace, mode="teammate", kind="teammate")

            lead_registry = build_lead_tool_registry()
            teammate_registry = build_teammate_tool_registry("alice")

            self.assertIn("rg", lead_registry.visible_names_for_turn(coding, "coding"))
            self.assertIn("rg", teammate_registry.visible_names_for_turn(teammate, "teammate"))


if __name__ == "__main__":
    unittest.main()
