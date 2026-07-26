import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from runtime.sessions.session import Session
from tools.handlers import configure_subagent_runner, make_lead_handlers


class FakeRunner:
    def __init__(self) -> None:
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            to_dict=lambda: {
                "agent_type": kwargs["agent_type"],
                "success": True,
                "summary": "done",
                "status": "completed",
            }
        )


class FakeTeam:
    def member_names(self):
        return []

    def spawn(self, name, role, prompt):
        return f"spawned {name}"

    def list_all(self):
        return "No teammates."


class SubagentDispatchValidationTests(unittest.TestCase):
    def tearDown(self) -> None:
        configure_subagent_runner(None)

    def test_missing_scope_file_is_allowed_to_subagent(self) -> None:
        runner = FakeRunner()
        configure_subagent_runner(runner)
        handlers = make_lead_handlers(FakeTeam())
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "frontend" / "dashboard").mkdir(parents=True)
            (workspace / "frontend" / "dashboard" / "build.mjs").write_text(
                "export {}\n",
                encoding="utf-8",
            )
            session = Session(
                id="parent",
                active_agent="coding",
                metadata={"user_role": "admin", "workspace_root": tmp},
            )
            task = {
                "prompt": "inspect frontend/dashboard/build.js",
                "description": "dashboard build",
                "agent_type": "explore",
                "scope": {"files": ["frontend/dashboard/build.js"]},
            }

            output = handlers["parallel_tasks"](tasks=[task], _session=session)

        payload = json.loads(output)
        self.assertEqual(1, len(payload["results"]))
        self.assertTrue(payload["results"][0]["success"])
        self.assertEqual(1, len(runner.calls))
        self.assertIn("frontend/dashboard/build.js", runner.calls[0]["prompt"])

    def test_prompt_only_file_hint_is_allowed_to_subagent(self) -> None:
        runner = FakeRunner()
        configure_subagent_runner(runner)
        handlers = make_lead_handlers(FakeTeam())
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "package.json").write_text("{}\n", encoding="utf-8")
            session = Session(
                id="parent",
                active_agent="coding",
                metadata={"user_role": "admin", "workspace_root": tmp},
            )
            task = {
                "prompt": "Inspect package.json and return findings.",
                "description": "package metadata",
                "agent_type": "explore",
            }

            output = handlers["parallel_tasks"](tasks=[task], _session=session)

        payload = json.loads(output)
        self.assertEqual(1, len(payload["results"]))
        self.assertTrue(payload["results"][0]["success"])
        self.assertEqual(1, len(runner.calls))
        self.assertIn("package.json", runner.calls[0]["prompt"])


if __name__ == "__main__":
    unittest.main()
