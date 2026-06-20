import subprocess
import tempfile
import unittest
from pathlib import Path

from sessions.session import Session
from tools import handlers
from tools.executor import ToolExecutionRequest, ToolExecutor
from tools.hooks import ShellWorkspaceScopeHook
from tools.tool_registry import build_lead_tool_registry


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)


def _session_for(root: Path) -> Session:
    return Session(
        id="task:git",
        current_mode="coding",
        metadata={
            "kind": "task_session",
            "workspace_root": str(root),
            "user_role": "admin",
        },
    )


class GitToolTests(unittest.TestCase):
    def test_git_status_diff_add_commit_and_log_use_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            workspace.mkdir()
            _init_repo(workspace)
            session = _session_for(workspace)

            (workspace / "README.md").write_text("hello\n", encoding="utf-8")

            status = handlers.run_git_status(_session=session)
            self.assertIn("README.md", status)

            add = handlers.run_git_add(["README.md"], _session=session)
            self.assertEqual("(no output)", add)

            staged_diff = handlers.run_git_diff(staged=True, _session=session)
            self.assertIn("+hello", staged_diff)

            commit = handlers.run_git_commit("initial commit", _session=session)
            self.assertIn("initial commit", commit)

            log = handlers.run_git_log(max_count=1, _session=session)
            self.assertIn("initial commit", log)

    def test_git_pathspecs_cannot_escape_workspace_or_use_magic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            workspace.mkdir()
            _init_repo(workspace)
            session = _session_for(workspace)

            outside = handlers.run_git_add(["../outside.txt"], _session=session)
            self.assertIn("Path escapes workspace", outside)

            magic = handlers.run_git_add([":(top)README.md"], _session=session)
            self.assertIn("Git pathspec magic is not allowed", magic)

    def test_git_tools_visibility_and_deferred_write_tools(self) -> None:
        registry = build_lead_tool_registry()
        session = _session_for(Path("/tmp/project"))

        visible = registry.visible_names_for_turn(session, "coding")
        self.assertIn("bash", visible)
        self.assertIn("edit_file", visible)
        self.assertIn("write_file", visible)
        self.assertIn("list_files", visible)
        self.assertIn("git_status", visible)
        self.assertIn("git_diff", visible)
        self.assertIn("git_log", visible)
        self.assertIn("git_branch", visible)
        self.assertNotIn("git_add", visible)
        self.assertNotIn("git_commit", visible)

        unlocked = registry.execute(
            "tool_search",
            {"query": "select:git_add"},
            session=session,
            mode="coding",
        )
        self.assertIn("Unlocked tool", unlocked)
        self.assertIn("git_add", registry.visible_names_for_turn(session, "coding"))

    def test_list_files_uses_workspace_and_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            (workspace / "src").mkdir(parents=True)
            (workspace / "src" / "version.py").write_text('VERSION = "0.1.0"\n', encoding="utf-8")
            session = _session_for(workspace)

            listing = handlers.run_list_files(recursive=True, _session=session)
            self.assertIn("src/version.py", listing)

            escaped = handlers.run_list_files("../outside", _session=session)
            self.assertIn("Path escapes workspace", escaped)

    def test_read_file_offset_returns_continuation_hint_and_cache_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            workspace.mkdir()
            (workspace / "long.txt").write_text(
                "\n".join(f"line {index}" for index in range(5)),
                encoding="utf-8",
            )
            session = _session_for(workspace)

            first = handlers.run_read("long.txt", limit=2, _session=session)
            self.assertIn("line 0", first)
            self.assertIn("line 1", first)
            self.assertIn("offset=2", first)
            self.assertIn("3 lines remain", first)

            second = handlers.run_read("long.txt", limit=2, offset=2, _session=session)
            self.assertIn("line 2", second)
            self.assertIn("line 3", second)
            self.assertIn("offset=4", second)

            cached = handlers.run_read("long.txt", limit=2, offset=2, _session=session)
            self.assertIn("[tool-cache] already read at step", cached)
            self.assertIn("line 2", cached)

    def test_subagent_large_read_returns_outline_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            workspace.mkdir()
            (workspace / "large.py").write_text(
                "\n".join(f"line {index}" for index in range(350)),
                encoding="utf-8",
            )
            session = _session_for(workspace)
            session.metadata["kind"] = "subagent"

            output = handlers.run_read("large.py", _session=session)

            self.assertIn("large file guard", output)
            self.assertIn('code_outline(path="large.py")', output)
            self.assertIn("offset=80", output)

    def test_parent_large_read_is_not_outline_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            workspace.mkdir()
            (workspace / "large.py").write_text(
                "\n".join(f"line {index}" for index in range(350)),
                encoding="utf-8",
            )
            session = _session_for(workspace)

            output = handlers.run_read("large.py", _session=session)

            self.assertNotIn("large file guard", output)
            self.assertIn("line 300", output)

    def test_list_files_offset_paginates_large_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            workspace.mkdir()
            for index in range(505):
                (workspace / f"file_{index:03d}.txt").write_text("x", encoding="utf-8")
            session = _session_for(workspace)

            first = handlers.run_list_files(".", offset=0, _session=session)
            self.assertIn('"truncated": true', first)
            self.assertIn('"next_offset": 500', first)
            self.assertIn("offset=500", first)

            second = handlers.run_list_files(".", offset=500, _session=session)
            self.assertIn('"truncated": false', second)
            self.assertIn("file_504.txt", second)

    def test_shell_workspace_scope_blocks_external_cd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            workspace.mkdir()
            inside = workspace / "subdir"
            inside.mkdir()
            session = _session_for(workspace)
            executor = ToolExecutor([ShellWorkspaceScopeHook(workspace)])

            blocked = executor.execute(
                ToolExecutionRequest(
                    call_id="call-1",
                    tool_name="bash",
                    arguments={"command": "cd /home/tale/kaggle/mytry && pwd"},
                    session_id=session.id,
                    metadata=session.metadata,
                ),
                lambda name, args: "should not run",
            )
            self.assertEqual("denied", blocked.status)
            self.assertIn("outside workspace", blocked.output)

            allowed = executor.execute(
                ToolExecutionRequest(
                    call_id="call-2",
                    tool_name="bash",
                    arguments={"command": f"cd {inside} && pwd"},
                    session_id=session.id,
                    metadata=session.metadata,
                ),
                lambda name, args: "ok",
            )
            self.assertEqual("success", allowed.status)
            self.assertEqual("ok", allowed.output)


if __name__ == "__main__":
    unittest.main()
