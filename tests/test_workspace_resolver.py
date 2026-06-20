import tempfile
import unittest
from pathlib import Path

from runtime.workspace import safe_workspace_path
from sessions.session import Session


class WorkspaceResolverTests(unittest.TestCase):
    def test_safe_workspace_path_requires_session(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires a session"):
            safe_workspace_path("README.md")

    def test_safe_workspace_path_rejects_absolute_paths_with_relative_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            source = workspace / "src"
            source.mkdir(parents=True)
            session = Session(
                id="task:workspace",
                metadata={"workspace_root": str(workspace)},
            )

            with self.assertRaisesRegex(
                ValueError,
                r"Absolute workspace paths are not allowed: .+ Use relative path 'src'",
            ):
                safe_workspace_path(str(source), session=session)

            with self.assertRaisesRegex(
                ValueError,
                r"Absolute workspace paths are not allowed: .+ Use '\.' for the workspace root",
            ):
                safe_workspace_path(str(workspace), session=session)

    def test_safe_workspace_path_blocks_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            workspace = Path(tmp)
            outside_file = Path(outside) / "secret.txt"
            outside_file.write_text("secret", encoding="utf-8")
            (workspace / "link.txt").symlink_to(outside_file)
            session = Session(
                id="task:workspace",
                metadata={"workspace_root": str(workspace)},
            )

            with self.assertRaisesRegex(ValueError, "escapes workspace"):
                safe_workspace_path("link.txt", session=session)


if __name__ == "__main__":
    unittest.main()
