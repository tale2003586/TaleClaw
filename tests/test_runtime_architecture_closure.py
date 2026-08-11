import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

from runtime.context import ContextBuilder
from runtime.execution.reasoning_loop import ReasoningLoop
from runtime.sessions import Session


ROOT = Path(__file__).resolve().parents[1]


class RuntimeArchitectureClosureTests(unittest.TestCase):
    def test_runtime_has_one_execution_entry(self):
        tree = ast.parse((ROOT / "runtime/runtime.py").read_text(encoding="utf-8"))
        runtime_class = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Runtime"
        )
        methods = {node.name for node in runtime_class.body if isinstance(node, ast.FunctionDef)}
        self.assertIn("run", methods)
        self.assertNotIn("run_turn", methods)
        self.assertNotIn("_run_turn", methods)

    def test_runtime_kernel_does_not_import_product_layers(self):
        forbidden = ("applications", "gateway", "web")
        violations = []
        for path in (ROOT / "runtime").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name.split(".", 1)[0] in forbidden:
                        violations.append(f"{path.relative_to(ROOT)}:{name}")
        self.assertEqual([], violations)

    def test_removed_runtime_modules_and_signature_adapters_stay_removed(self):
        for name in ("working_memory.py", "agent_loop.py", "app_runtime.py", "bootstrap.py"):
            self.assertFalse((ROOT / "runtime" / name).exists())
        runtime_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "runtime").rglob("*.py")
        )
        self.assertNotIn("inspect.signature", runtime_source)

    def test_plain_bot_context_does_not_create_task_state(self):
        session = Session(id="chat:plain", active_agent="bot")
        session.add_message("user", "hello")
        agent_spec = SimpleNamespace(
            name="bot",
            tool_mode="bot",
            instructions="You are helpful.",
        )
        ContextBuilder().build(session=session, agent_spec=agent_spec)
        self.assertNotIn("task_state", session.metadata)

    def test_reasoning_checkpoint_is_controlled_by_explicit_callback(self):
        session = Session(id="chat:checkpoint", active_agent="bot")
        events = []
        ReasoningLoop(tools=None, tool_executor=None)._checkpoint_reasoning_step(
            session,
            SimpleNamespace(tool_mode="bot"),
            step=1,
            phase="assistant_final",
            checkpoint_callback=lambda _: events.append("checkpoint"),
        )
        self.assertEqual(["checkpoint"], events)
        self.assertTrue(any(event.type == "run_checkpoint" for event in session.event_log))

    def test_runtime_has_no_coding_bus_or_background_fields(self):
        source = (ROOT / "runtime/runtime.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        assigned_attributes = {
            target.attr
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        }
        self.assertTrue({"background_results", "bus", "coding_application"}.isdisjoint(assigned_attributes))


if __name__ == "__main__":
    unittest.main()
