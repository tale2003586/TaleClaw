import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agents.subagent.orchestration_state import ORCHESTRATION_STATE_KEY
from agents.subagent.runner import TaskSubagentRunner
from agents.definitions import CODING_AGENT_SPEC
from runtime.runtime import Runtime
from runtime.agent_spec import AgentSpec, SpawnPolicy, ToolSet
from runtime.sessions.session import Session
from tools.executor import ToolExecutor
from tools.handlers import make_lead_handlers
from tools.schema import function_tool
from tools.tool_registry import (
    ToolRegistry,
    build_lead_tool_registry,
    register_lead_subagent_tools,
)
from tools.spec import ToolSpec
from models.provider import LLMResponse, ToolCall


class ContextBuilder:
    def build(self, **kwargs):
        return SimpleNamespace(messages=kwargs["session"].messages)


class DummyProvider:
    pass


class RecordingProvider:
    def __init__(self, responses) -> None:
        self.responses = list(responses)

    def chat(self, **_kwargs):
        return self.responses.pop(0)


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


def _runtime(registry: ToolRegistry) -> Runtime:
    return Runtime(
        tools=registry,
        provider=DummyProvider(),
        model="test-model",
        tool_executor=ToolExecutor([]),
        context_builder=ContextBuilder(),
    )


class SubagentToolTests(unittest.TestCase):
    def test_subagent_tool_filter_excludes_team_tools(self) -> None:
        runner = TaskSubagentRunner(base_runtime=_runtime(_registry()))

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

    def test_parallel_tasks_are_deferred_and_high_value_readers_are_preloaded(self) -> None:
        registry = build_lead_tool_registry(FakeTeam())
        session = Session(
            id="task:parent",
            active_agent="coding",
            metadata={"user_role": "admin"},
        )

        visible = registry.visible_names_for_turn(session, "coding")

        self.assertNotIn("repo_map", visible)
        self.assertIn("rg", visible)
        self.assertNotIn("grep", visible)
        self.assertNotIn("nl", visible)
        self.assertIn("code_outline", visible)
        self.assertIn("read_files", visible)
        self.assertNotIn("parallel_tasks", visible)
        self.assertNotIn("task", visible)
        self.assertIn(
            "parallel_tasks",
            registry.execute(
                "tool_search",
                {"query": "parallel"},
                session=session,
                mode="coding",
            ),
        )
        self.assertIn("Use rg/list_files/read_file", CODING_AGENT_SPEC.instructions)

    def test_two_stage_registry_assembly_binds_runner_to_the_existing_runtime(self) -> None:
        registry = build_lead_tool_registry(
            FakeTeam(),
            include_subagent_tools=False,
        )
        runtime = _runtime(registry)
        runner = TaskSubagentRunner(base_runtime=runtime)

        self.assertNotIn("task", registry._tools)
        register_lead_subagent_tools(registry, runner)

        self.assertIs(registry, runtime.agent_runner.tools)
        self.assertIn("task", registry._tools)
        self.assertIn("parallel_tasks", registry._tools)
        self.assertEqual(
            "Unknown tool: task",
            build_lead_tool_registry(
                FakeTeam(),
                include_subagent_tools=False,
            ).execute("task", {}, mode="coding"),
        )
        with self.assertRaisesRegex(ValueError, "requires a constructed subagent runner"):
            register_lead_subagent_tools(registry, None)

    def test_runner_bound_handlers_do_not_cross_contaminate_registries(self) -> None:
        first = FakeRunner()
        second = FakeRunner()
        first_registry = build_lead_tool_registry(FakeTeam(), include_subagent_tools=False)
        second_registry = build_lead_tool_registry(FakeTeam(), include_subagent_tools=False)
        register_lead_subagent_tools(first_registry, first)
        register_lead_subagent_tools(second_registry, second)

        first_registry.spec_for("task").handler(
            prompt="first", agent_type="explore", _session=SimpleNamespace(id="first"),
        )
        second_registry.spec_for("task").handler(
            prompt="second", agent_type="explore", _session=SimpleNamespace(id="second"),
        )

        self.assertEqual("first", first.calls[0]["prompt"])
        self.assertEqual("second", second.calls[0]["prompt"])

    def test_agent_runner_reads_late_registered_task_tool_during_a_turn(self) -> None:
        registry = build_lead_tool_registry(FakeTeam(), include_subagent_tools=False)
        provider = RecordingProvider([
            LLMResponse(
                content=None,
                tool_calls=[ToolCall(
                    id="task-1",
                    name="task",
                    arguments={"prompt": "inspect auth", "agent_type": "explore"},
                )],
                raw_message={"role": "assistant", "content": None},
            ),
            LLMResponse(content="done", raw_message={"role": "assistant", "content": "done"}),
        ])
        runtime = Runtime(
            tools=registry,
            provider=provider,
            model="test-model",
            tool_executor=ToolExecutor([]),
            context_builder=ContextBuilder(),
        )
        runner = FakeRunner()
        register_lead_subagent_tools(registry, runner)
        session = Session(
            id="late-registration",
            metadata={"user_role": "admin", "unlocked_tools": ["task"]},
        )
        session.add_message("user", "delegate this")

        runtime.agent_runner.run(
            session=session,
            spec=AgentSpec(
                name="coding",
                tool_set=ToolSet(mode="coding"),
                spawn_policy=SpawnPolicy(
                    enabled=True,
                    allowed_agent_types=("explore",),
                ),
            ),
        )

        self.assertEqual("inspect auth", runner.calls[0]["prompt"])
        self.assertEqual("done", session.messages[-1]["content"])

    def test_task_handler_invokes_configured_runner(self) -> None:
        fake_runner = FakeRunner()
        handlers = make_lead_handlers(FakeTeam(), subagent_runner=fake_runner)

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
        handlers = make_lead_handlers(FakeTeam(), subagent_runner=fake_runner)
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
        handlers = make_lead_handlers(FakeTeam(), subagent_runner=fake_runner)
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
        handlers = make_lead_handlers(FakeTeam(), subagent_runner=fake_runner)
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
        handlers = make_lead_handlers(FakeTeam(), subagent_runner=failing_runner)
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
