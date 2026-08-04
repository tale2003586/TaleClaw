import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agents.subagent.orchestration_state import ORCHESTRATION_STATE_KEY
from agents.subagent.runner import TaskSubagentRunner
from agents.definitions import CODING_AGENT_SPEC
from runtime.runtime import Runtime
from runtime.sessions.session import Session
from tools.executor import ToolExecutor
from tools.handlers import configure_subagent_runner, make_lead_handlers
from tools.schema import function_tool
from tools.tool_registry import ToolRegistry, build_lead_tool_registry
from tools.spec import ToolSpec


class ContextBuilder:
    def build(self, **kwargs):
        return SimpleNamespace(messages=kwargs["session"].messages)


class DummyProvider:
    pass


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
                "files_touched": [],
                "tool_count": 0,
                "error": None,
            }
        )


class FailingRunner:
    def __init__(self) -> None:
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            to_dict=lambda: {
                "agent_type": kwargs["agent_type"],
                "success": False,
                "summary": "",
                "status": "failed",
                "files_touched": [],
                "tool_count": 0,
                "error": "step limit",
                "truncated": True,
                "stop_reason": "reasoning_step_limit",
                "findings": [],
                "incomplete": True,
                "failure_reason": "subagent_step_limit",
                "failure_message": "hit step limit",
                "recoverable": True,
                "retry_hint": "split narrower",
                "evidence": [],
            }
        )


class FakeTeam:
    def member_names(self):
        return []

    def spawn(self, name, role, prompt):
        return f"spawned {name}"

    def list_all(self):
        return "No teammates."


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name in [
        "read_file",
        "read_files",
        "rg",
        "grep",
        "nl",
        "code_outline",
        "edit_file",
        "task",
        "spawn_teammate",
        "tool_search",
    ]:
        registry.register(ToolSpec(
            schema=function_tool(name, f"{name} tool", {}, []),
            handler=lambda **kwargs: "ok",
            allowed_modes=frozenset({"coding"}),
        ))
    return registry


def _pipeline(registry: ToolRegistry) -> Runtime:
    return Runtime(
        tools=registry,
        provider=DummyProvider(),
        model="test-model",
        tool_executor=ToolExecutor([]),
        context_builder=ContextBuilder(),
    )


class SubagentToolTests(unittest.TestCase):
    def tearDown(self) -> None:
        configure_subagent_runner(None)

    def test_subagent_tool_filter_excludes_team_tools(self) -> None:
        runner = TaskSubagentRunner(base_pipeline=_pipeline(_registry()))

        tools = runner._filtered_tools("code")

        self.assertIn("read_file", tools._tools)
        self.assertIn("read_files", tools._tools)
        self.assertIn("rg", tools._tools)
        self.assertIn("grep", tools._tools)
        self.assertIn("nl", tools._tools)
        self.assertIn("code_outline", tools._tools)
        self.assertIn("edit_file", tools._tools)
        self.assertIn("tool_search", tools._tools)
        self.assertNotIn("task", tools._tools)
        self.assertNotIn("spawn_teammate", tools._tools)

    def test_parallel_tasks_is_preloaded_for_coding_agent(self) -> None:
        registry = build_lead_tool_registry(FakeTeam())
        session = Session(
            id="task:parent",
            active_agent="coding",
            metadata={"user_role": "admin"},
        )

        visible = registry.visible_names_for_turn(session, "coding")

        self.assertIn("repo_map", visible)
        self.assertIn("rg", visible)
        self.assertIn("grep", visible)
        self.assertIn("nl", visible)
        self.assertIn("code_outline", visible)
        self.assertIn("read_files", visible)
        self.assertIn("parallel_tasks", visible)
        self.assertIn("task", visible)
        self.assertIn("parallel_tasks", registry.tool_catalog_text(session, "coding"))
        self.assertIn("Build a deterministic file map first with repo_map", CODING_AGENT_SPEC.system_prompt)

    def test_task_handler_invokes_configured_runner(self) -> None:
        fake_runner = FakeRunner()
        configure_subagent_runner(fake_runner)
        handlers = make_lead_handlers(FakeTeam())

        output = handlers["task"](
            prompt="inspect auth",
            description="auth review",
            agent_type="explore",
            scope={"files": ["agents/subagent/runner.py"]},
            _session=SimpleNamespace(id="parent"),
        )
        payload = json.loads(output)

        self.assertTrue(payload["success"])
        self.assertEqual("explore", payload["agent_type"])
        self.assertIn('"agents/subagent/runner.py"', fake_runner.calls[0]["prompt"])
        self.assertTrue(fake_runner.calls[0]["prompt"].endswith("inspect auth"))
        self.assertEqual("parent", fake_runner.calls[0]["parent_session"].id)

    def test_task_handler_allows_missing_scope_file_to_runner(self) -> None:
        fake_runner = FakeRunner()
        configure_subagent_runner(fake_runner)
        handlers = make_lead_handlers(FakeTeam())
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(
                id="parent",
                metadata={"user_role": "admin", "workspace_root": tmp},
            )

            output = handlers["task"](
                prompt="inspect missing file",
                description="missing file",
                agent_type="explore",
                scope={"files": ["missing.py"]},
                _session=session,
            )

        payload = json.loads(output)
        self.assertTrue(payload["success"])
        self.assertEqual(1, len(fake_runner.calls))
        self.assertIn('"missing.py"', fake_runner.calls[0]["prompt"])
        self.assertEqual(
            0,
            session.metadata[ORCHESTRATION_STATE_KEY].get("fanout_rejected_count", 0),
        )

    def test_parallel_tasks_allows_directory_scope_to_runner(self) -> None:
        fake_runner = FakeRunner()
        configure_subagent_runner(fake_runner)
        handlers = make_lead_handlers(FakeTeam())
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "pkg").mkdir()
            session = Session(
                id="parent",
                metadata={"user_role": "admin", "workspace_root": tmp},
            )
            task = {
                "prompt": "inspect package",
                "description": "package clue",
                "agent_type": "explore",
                "scope": {"files": ["pkg"]},
            }

            output = handlers["parallel_tasks"](tasks=[task], _session=session)

        payload = json.loads(output)
        self.assertEqual(1, len(payload["results"]))
        self.assertTrue(payload["results"][0]["success"])
        self.assertEqual(1, len(fake_runner.calls))
        self.assertIn('"pkg"', fake_runner.calls[0]["prompt"])

    def test_parallel_tasks_allows_many_scope_files_to_runner(self) -> None:
        fake_runner = FakeRunner()
        configure_subagent_runner(fake_runner)
        handlers = make_lead_handlers(FakeTeam())
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for index in range(6):
                path = Path(tmp, f"file_{index}.py")
                path.write_text("pass\n", encoding="utf-8")
                files.append(path.name)
            session = Session(
                id="parent",
                metadata={"user_role": "admin", "workspace_root": tmp},
            )
            task = {
                "prompt": "inspect files",
                "description": "wide clue",
                "agent_type": "explore",
                "scope": {"files": files},
            }

            output = handlers["parallel_tasks"](tasks=[task], _session=session)

        payload = json.loads(output)
        self.assertEqual(1, len(payload["results"]))
        self.assertTrue(payload["results"][0]["success"])
        self.assertEqual(1, len(fake_runner.calls))
        for file_path in files:
            self.assertIn(file_path, fake_runner.calls[0]["prompt"])

    def test_parallel_tasks_handler_rejects_repeated_failed_clue(self) -> None:
        failing_runner = FailingRunner()
        configure_subagent_runner(failing_runner)
        handlers = make_lead_handlers(FakeTeam())
        session = Session(id="parent", metadata={"user_role": "admin"})
        task = {
            "prompt": "locate memory facts",
            "description": "memory clue",
            "agent_type": "explore",
            "scope": {"files": ["memory/store.py"]},
        }

        handlers["parallel_tasks"](tasks=[task], _session=session)
        handlers["parallel_tasks"](tasks=[task], _session=session)
        output = handlers["parallel_tasks"](tasks=[task], _session=session)
        payload = json.loads(output)

        self.assertFalse(payload["success"])
        self.assertEqual("subagent_orchestration_rejected", payload["failure_reason"])
        self.assertEqual([], payload["results"])
        self.assertEqual(2, len(failing_runner.calls))


if __name__ == "__main__":
    unittest.main()
