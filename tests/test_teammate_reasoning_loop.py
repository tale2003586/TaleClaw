import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from coding_runtime.teammate import TeammateContextBuilder, TeammateManager
from models.provider import LLMResponse, ToolCall
from sessions import Session
from tools.executor import ToolExecutor
from tools.tool_registry import build_teammate_tool_registry


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("No fake response queued.")
        return self.responses.pop(0)


class TeammateToolRegistryTests(unittest.TestCase):
    def test_teammate_tools_use_visibility_and_tool_search(self):
        registry = build_teammate_tool_registry("alice")
        session = Session(
            id="teammate:alice",
            current_mode="teammate",
            metadata={"kind": "teammate", "user_role": "admin"},
        )

        visible = registry.visible_names_for_turn(session, "teammate")

        self.assertIn("idle", visible)
        self.assertIn("rg", visible)
        self.assertIn("read_file", visible)
        self.assertIn("send_message", visible)
        self.assertIn("tool_search", visible)
        self.assertNotIn("bash", visible)
        self.assertEqual(
            registry.execution_error_for_turn(
                "spawn_teammate",
                session=session,
                mode="teammate",
            ),
            "Unknown tool: spawn_teammate",
        )

        output = registry.execute(
            "bash",
            {"command": "pwd"},
            session=session,
            mode="teammate",
        )
        self.assertIn("not visible", output)

        unlock = registry.execute(
            "tool_search",
            {"query": "select:bash"},
            session=session,
            mode="teammate",
        )
        self.assertIn("Unlocked tool", unlock)
        self.assertIn("bash", registry.visible_names_for_turn(session, "teammate"))


class TeammateReasoningLoopTests(unittest.TestCase):
    def test_idle_tool_stops_current_teammate_cycle(self):
        response = LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="call_idle", name="idle", arguments={})],
            raw_message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_idle",
                        "type": "function",
                        "function": {"name": "idle", "arguments": "{}"},
                    }
                ],
            },
        )
        provider = FakeProvider([response])

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = TeammateManager(Path(tmpdir) / ".team")
            manager.configure(
                provider=provider,
                model="fake-model",
                model_pool=None,
                tool_executor=ToolExecutor([]),
                max_reasoning_steps=5,
            )
            session = manager._new_session("alice", "tester", "Finish and idle.")
            profile = manager._profile_for("alice", "tester", "default")
            spec = manager._agent_spec(name="alice", role="tester", profile=profile)
            registry = build_teammate_tool_registry("alice")
            with contextlib.redirect_stdout(io.StringIO()):
                state = manager._run_reasoning_cycle(
                    name="alice",
                    session=session,
                    spec=spec,
                    tools=registry,
                    context_builder=TeammateContextBuilder("alice"),
                )

        self.assertTrue(state.should_idle)
        self.assertFalse(state.should_shutdown)
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(any(
            message.get("role") == "tool"
            and message.get("tool_call_id") == "call_idle"
            and "Entering idle" in message.get("content", "")
            for message in session.messages
        ))


if __name__ == "__main__":
    unittest.main()
