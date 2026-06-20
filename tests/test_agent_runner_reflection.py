import unittest
from types import SimpleNamespace

from runtime.agent_runner import AgentRunner
from runtime.agent_spec import AgentSpec
from models.provider import LLMResponse, ToolCall
from runtime.reflection import ReflectionAgent, ReflectionDecision
from modes.base import ModeProfile
from sessions import Session
from tools.executor import ToolExecutor
from tools.schema import function_tool
from tools.tool_registry import ToolRegistry


class ContextBuilder:
    def build(self, **kwargs):
        return SimpleNamespace(messages=kwargs["session"].messages)


class RecordingProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("No fake response queued.")
        return self.responses.pop(0)


class FakeModelPool:
    def __init__(self, provider):
        self.provider = provider
        self.purposes = []

    def routed_provider(self, purpose):
        self.purposes.append(("provider", purpose))
        return self.provider

    def model_for(self, purpose):
        self.purposes.append(("model", purpose))
        return f"{purpose}-model"


class FakeReflectionAgent:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def should_reflect(self, **kwargs):
        self.calls.append(("should", kwargs))
        return True

    def reflect(self, **kwargs):
        self.calls.append(("reflect", kwargs))
        return self.decision


def _profile(tool_mode="bot"):
    return ModeProfile(
        name=tool_mode,
        tool_mode=tool_mode,
        system_prompt="test profile",
    )


def _registry():
    registry = ToolRegistry()
    registry.register(
        function_tool("echo", "Echo test tool", {"text": {"type": "string"}}, ["text"]),
        lambda **kwargs: f"echo: {kwargs['text']}",
        enabled_modes={"bot", "coding", "teammate"},
        always_on=True,
    )
    return registry


def _tool_response(index: int, name="echo", arguments=None):
    arguments = arguments or {"text": "hello"}
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id=f"call-{index}", name=name, arguments=arguments)],
        raw_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"call-{index}",
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }],
        },
    )


def _final_response(content="done"):
    return LLMResponse(
        content=content,
        raw_message={"role": "assistant", "content": content},
    )


class AgentRunnerTests(unittest.TestCase):
    def test_agent_runner_routes_by_agent_spec_model_purpose(self):
        provider = RecordingProvider([_final_response("ok")])
        model_pool = FakeModelPool(provider)
        runner = AgentRunner(
            tools=_registry(),
            tool_executor=ToolExecutor([]),
            model_pool=model_pool,
            context_builder=ContextBuilder(),
        )
        session = Session(id="agent:test")
        session.add_message("user", "hello")
        spec = AgentSpec(
            name="worker",
            profile=_profile("bot"),
            model_purpose="teammate",
        )

        runner.run_turn(session=session, spec=spec)

        self.assertEqual("teammate-model", provider.calls[0]["model"])
        self.assertIn(("provider", "teammate"), model_pool.purposes)
        self.assertIn(("model", "teammate"), model_pool.purposes)

    def test_reflection_instruction_is_added_before_next_reasoning_step(self):
        provider = RecordingProvider([
            _tool_response(1),
            _final_response("done"),
        ])
        reflection = FakeReflectionAgent(ReflectionDecision(
            action="revise",
            instruction="Use the existing echo result and stop calling tools.",
        ))
        runner = AgentRunner(
            tools=_registry(),
            tool_executor=ToolExecutor([]),
            provider=provider,
            model="test-model",
            context_builder=ContextBuilder(),
            reflection_agent=reflection,
        )
        session = Session(id="agent:reflect")
        session.add_message("user", "do it")
        spec = AgentSpec(name="main", profile=_profile("bot"), model_purpose="chat")

        runner.run_turn(session=session, spec=spec)

        self.assertEqual("done", session.messages[-1]["content"])
        self.assertEqual(2, len(provider.calls))
        second_messages = provider.calls[1]["messages"]
        self.assertTrue(any(
            "<reflection-instruction" in str(message.get("content") or "")
            and 'critical="true"' in str(message.get("content") or "")
            for message in second_messages
        ))
        self.assertEqual(["should", "reflect"], [item[0] for item in reflection.calls])

    def test_reflection_stop_ends_turn_with_guard_message(self):
        provider = RecordingProvider([_tool_response(1)])
        reflection = FakeReflectionAgent(ReflectionDecision(
            action="stop",
            reason="tool loop is not productive",
            message="Reflection stopped this turn.",
        ))
        runner = AgentRunner(
            tools=_registry(),
            tool_executor=ToolExecutor([]),
            provider=provider,
            model="test-model",
            context_builder=ContextBuilder(),
            reflection_agent=reflection,
        )
        session = Session(id="agent:stop")
        session.add_message("user", "do it")
        spec = AgentSpec(name="main", profile=_profile("bot"), model_purpose="chat")

        runner.run_turn(session=session, spec=spec)

        self.assertEqual(1, len(provider.calls))
        self.assertEqual("Reflection stopped this turn.", session.messages[-1]["content"])
        self.assertEqual("agent_loop_guard", session.messages[-1]["metadata"]["kind"])
        self.assertEqual("reflection_stop", session.messages[-1]["metadata"]["reason"])


class ReflectionAgentTests(unittest.TestCase):
    def test_reflection_agent_parses_markdown_fenced_json_decision(self):
        provider = RecordingProvider([
            LLMResponse(
                content='```json\n{"action":"ask_user","message":"Need approval."}\n```',
                raw_message={"role": "assistant", "content": ""},
            )
        ])
        agent = ReflectionAgent(provider=provider, model="reflect-model")
        execution = SimpleNamespace(
            loop_guard_denied=False,
            unavailable_tools=[],
            tool_results=[{"name": "echo", "status": "error", "output": "bad"}],
        )

        decision = agent.reflect(
            session=Session(id="reflect:test"),
            profile=_profile("bot"),
            response=_final_response(""),
            execution=execution,
            reasoning_steps=3,
        )

        self.assertEqual("ask_user", decision.action)
        self.assertEqual("Need approval.", decision.message)
        self.assertEqual("reflect-model", provider.calls[0]["model"])


if __name__ == "__main__":
    unittest.main()
