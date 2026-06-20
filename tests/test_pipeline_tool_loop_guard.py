import unittest
from types import SimpleNamespace

from plugins.web_search.plugin import WebSearchPlugin
from runtime.pipeline import Pipeline
from models.provider import LLMResponse, ToolCall
from sessions.session import Session
from tools.executor import ToolExecutor
from tools.hooks import ToolLoopGuardHook
from tools.schema import function_tool
from tools.tool_registry import ToolRegistry


class ContextBuilder:
    def build(self, **kwargs):
        return SimpleNamespace(messages=kwargs["session"].messages)


class RepeatingProvider:
    def __init__(self, name: str, arguments=None) -> None:
        self.name = name
        self.arguments = arguments or {}
        self.calls = 0

    def chat(self, **kwargs):
        self.calls += 1
        arguments = (
            self.arguments(self.calls)
            if callable(self.arguments)
            else self.arguments
        )
        return _tool_response(self.calls, self.name, arguments)


class ScriptedProvider:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses[self.calls]
        self.calls += 1
        return response


class TraceStore:
    def __init__(self) -> None:
        self.events = []

    def append_event(self, run_state, event_name, payload, **kwargs):
        self.events.append((event_name, payload, kwargs))

    def write_run_state(self, run_state):
        return None


class RunState:
    run_id = "run-test"

    def record_reasoning_step(self, step):
        return None

    def record_tool(self, name):
        return None

    def stop(self, reason, message):
        return None


def _tool_response(index: int, name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(
            id=f"call-{index}",
            name=name,
            arguments=arguments,
        )],
        raw_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"call-{index}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": "{}",
                },
            }],
        },
    )


def _final_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        raw_message={
            "role": "assistant",
            "content": content,
        },
    )


def _empty_response() -> LLMResponse:
    return LLMResponse(
        content="",
        raw_message={
            "role": "assistant",
            "content": "",
        },
    )


def _registry_with_tool(
    name: str,
    *,
    enabled_modes: set[str],
    always_on: bool = False,
    handler=None,
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        function_tool(name, f"{name} test tool", {}, []),
        handler or (lambda **kwargs: "ok"),
        enabled_modes=enabled_modes,
        always_on=always_on,
    )
    return registry


def _pipeline(registry, provider, *, hooks=None, max_reasoning_steps=24) -> Pipeline:
    return Pipeline(
        tools=registry,
        provider=provider,
        model="test-model",
        tool_executor=ToolExecutor(hooks or []),
        context_builder=ContextBuilder(),
        max_reasoning_steps=max_reasoning_steps,
    )


class PipelineToolLoopGuardTests(unittest.TestCase):
    def test_web_search_tool_description_mentions_runtime_budget(self) -> None:
        schema = WebSearchPlugin().tools()[0].schema

        self.assertIn("per-turn runtime budget", schema["function"]["description"])
        self.assertIn("web-search-budget", schema["function"]["description"])

    def test_empty_model_response_retries_then_stops(self) -> None:
        registry = _registry_with_tool("read_file", enabled_modes={"coding"}, always_on=True)
        provider = ScriptedProvider([
            _empty_response(),
            _empty_response(),
        ])
        pipeline = _pipeline(registry, provider, max_reasoning_steps=4)
        session = Session(id="task:test", current_mode="coding")

        reply = pipeline.run(session, SimpleNamespace(tool_mode="coding"))

        self.assertEqual(2, provider.calls)
        self.assertIn("模型连续返回空回复", reply)
        self.assertEqual("agent_loop_guard", session.messages[-1]["metadata"]["kind"])
        self.assertEqual("empty_model_response", session.messages[-1]["metadata"]["reason"])
        self.assertTrue(
            any(
                message.get("metadata", {}).get("reason") == "empty_model_response"
                for message in session.messages
                if message.get("role") == "user"
            )
        )

    def test_repeated_invisible_tool_request_stops_turn(self) -> None:
        registry = _registry_with_tool("git_add", enabled_modes={"coding"})
        provider = RepeatingProvider("git_add", {"paths": ["README.md"]})
        pipeline = _pipeline(registry, provider)
        session = Session(id="task:test", current_mode="coding")

        reply = pipeline.run(session, SimpleNamespace(tool_mode="coding"))

        self.assertEqual(2, provider.calls)
        self.assertIn("重复请求当前不可用的工具 `git_add`", reply)
        self.assertEqual("agent_loop_guard", session.messages[-1]["metadata"]["kind"])
        self.assertEqual("unavailable_tool_loop", session.messages[-1]["metadata"]["reason"])
        tool_outputs = [
            message["content"]
            for message in session.messages
            if message["role"] == "tool"
        ]
        self.assertEqual(2, len(tool_outputs))
        self.assertTrue(all("not visible in this turn" in output for output in tool_outputs))

    def test_tool_search_unlock_then_deferred_tool_call_still_completes(self) -> None:
        registry = _registry_with_tool("git_add", enabled_modes={"coding"})
        registry.register(
            function_tool("tool_search", "search tools", {}, []),
            lambda **kwargs: "registry handles this",
            enabled_modes={"coding"},
        )
        git_add_calls = []
        registry._tools["git_add"].handler = lambda **kwargs: git_add_calls.append(kwargs) or "done"
        provider = ScriptedProvider([
            _tool_response(1, "tool_search", {"query": "select:git_add"}),
            _tool_response(2, "git_add", {"paths": ["README.md"]}),
            _final_response("completed"),
        ])
        pipeline = _pipeline(registry, provider)
        session = Session(id="task:test", current_mode="coding")

        reply = pipeline.run(session, SimpleNamespace(tool_mode="coding"))

        self.assertEqual("completed", reply)
        self.assertEqual(3, provider.calls)
        self.assertEqual(["README.md"], git_add_calls[0]["paths"])
        self.assertIs(session, git_add_calls[0]["_session"])
        self.assertEqual(["git_add"], session.metadata["unlocked_tools"])

    def test_tool_search_help_returns_allowed_tool_schema(self) -> None:
        registry = _registry_with_tool("git_add", enabled_modes={"coding"})
        registry.register(
            function_tool("tool_search", "search tools", {}, []),
            lambda **kwargs: "registry handles this",
            enabled_modes={"coding"},
        )
        session = Session(id="task:test", current_mode="coding")

        output = registry.execute(
            "tool_search",
            {"query": "help:git_add"},
            session=session,
            mode="coding",
        )

        self.assertIn("Tool: git_add", output)
        self.assertIn("Parameters:", output)

    def test_pipeline_includes_tool_catalog_in_model_context(self) -> None:
        registry = _registry_with_tool(
            "echo",
            enabled_modes={"bot"},
            always_on=True,
        )
        provider = ScriptedProvider([_final_response("done")])
        pipeline = _pipeline(registry, provider)
        session = Session(id="web:test", current_mode="bot")

        reply = pipeline.run(session, SimpleNamespace(tool_mode="bot"))

        self.assertEqual("done", reply)
        sent_messages = provider.requests[0]["messages"]
        self.assertIn("<tool_catalog>", sent_messages[-1]["content"])
        self.assertIn("echo", sent_messages[-1]["content"])

    def test_tool_loop_hook_denial_stops_turn(self) -> None:
        registry = _registry_with_tool(
            "echo",
            enabled_modes={"bot"},
            always_on=True,
        )
        provider = RepeatingProvider("echo", {"text": "same"})
        pipeline = _pipeline(
            registry,
            provider,
            hooks=[ToolLoopGuardHook(repeat_limit=3)],
        )
        session = Session(id="web:test", current_mode="bot")

        reply = pipeline.run(session, SimpleNamespace(tool_mode="bot"))

        self.assertEqual(3, provider.calls)
        self.assertIn("重复调用同一工具", reply)
        self.assertEqual("repeated_tool_call", session.messages[-1]["metadata"]["reason"])

    def test_no_information_gain_result_denial_stops_turn(self) -> None:
        registry = _registry_with_tool(
            "read_file",
            enabled_modes={"bot"},
            always_on=True,
            handler=lambda **kwargs: "same result",
        )
        provider = RepeatingProvider("read_file", lambda index: {"path": f"{index}.py"})
        pipeline = _pipeline(
            registry,
            provider,
            hooks=[ToolLoopGuardHook(repeat_limit=10, result_repeat_limit=3)],
        )
        session = Session(id="web:test", current_mode="bot")

        reply = pipeline.run(session, SimpleNamespace(tool_mode="bot"))

        self.assertEqual(3, provider.calls)
        self.assertIn("重复调用同一工具", reply)
        self.assertEqual("repeated_tool_call", session.messages[-1]["metadata"]["reason"])
        denied = [
            message for message in session.messages
            if message.get("role") == "tool" and message.get("status") == "denied"
        ]
        self.assertEqual(1, len(denied))
        self.assertIn("no-information-gain", denied[0]["content"])

    def test_reasoning_step_limit_stops_argument_churn(self) -> None:
        registry = _registry_with_tool(
            "echo",
            enabled_modes={"bot"},
            always_on=True,
        )
        provider = RepeatingProvider("echo", lambda index: {"index": index})
        pipeline = _pipeline(registry, provider, max_reasoning_steps=3)
        session = Session(id="web:test", current_mode="bot")

        reply = pipeline.run(session, SimpleNamespace(tool_mode="bot"))

        self.assertEqual(3, provider.calls)
        self.assertIn("工具推理步骤超过上限 (3)", reply)
        self.assertEqual("reasoning_step_limit", session.messages[-1]["metadata"]["reason"])

    def test_finishing_reminder_is_injected_near_step_budget(self) -> None:
        registry = _registry_with_tool(
            "echo",
            enabled_modes={"bot"},
            always_on=True,
        )
        provider = ScriptedProvider([
            _tool_response(1, "echo", {"text": "first"}),
            _tool_response(2, "echo", {"text": "second"}),
            _final_response("done"),
        ])
        pipeline = _pipeline(registry, provider, max_reasoning_steps=4)
        session = Session(id="web:test", current_mode="bot")
        trace = TraceStore()

        reply = pipeline.run(
            session,
            SimpleNamespace(tool_mode="bot"),
            run_state=RunState(),
            trace_store=trace,
        )

        self.assertEqual("done", reply)
        self.assertEqual(3, provider.calls)
        reminder_messages = [
            message
            for message in session.messages
            if message.get("metadata", {}).get("kind") == "runtime_finishing_reminder"
        ]
        self.assertEqual(1, len(reminder_messages))
        self.assertEqual("system", reminder_messages[0]["role"])
        self.assertIn("3/4 reasoning steps", reminder_messages[0]["content"])
        third_request = provider.requests[2]["messages"]
        self.assertTrue(
            any(
                message.get("metadata", {}).get("kind") == "runtime_finishing_reminder"
                for message in third_request
            )
        )
        self.assertTrue(
            any(
                event_name == "reasoning.finishing_reminder.injected"
                and payload["step"] == 3
                for event_name, payload, _ in trace.events
            )
        )

    def test_tool_loop_history_resets_between_user_turns(self) -> None:
        registry = _registry_with_tool(
            "echo",
            enabled_modes={"bot"},
            always_on=True,
        )
        provider = ScriptedProvider([
            _tool_response(1, "echo", {"text": "same"}),
            _final_response("first completed"),
            _tool_response(2, "echo", {"text": "same"}),
            _final_response("second completed"),
        ])
        pipeline = _pipeline(
            registry,
            provider,
            hooks=[ToolLoopGuardHook(repeat_limit=2)],
        )
        session = Session(id="web:test", current_mode="bot")

        first = pipeline.run(session, SimpleNamespace(tool_mode="bot"))
        second = pipeline.run(session, SimpleNamespace(tool_mode="bot"))

        self.assertEqual("first completed", first)
        self.assertEqual("second completed", second)
        self.assertEqual(4, provider.calls)

    def test_web_search_budget_notice_is_prepended_to_search_result(self) -> None:
        registry = _registry_with_tool(
            "web_search",
            enabled_modes={"bot"},
            always_on=True,
            handler=lambda **kwargs: "search result",
        )
        provider = ScriptedProvider([
            _tool_response(1, "web_search", {"query": "one"}),
            _final_response("done"),
        ])
        pipeline = _pipeline(registry, provider)
        session = Session(id="web:test", current_mode="bot")

        reply = pipeline.run(session, SimpleNamespace(tool_mode="bot"))

        self.assertEqual("done", reply)
        self.assertEqual(2, provider.calls)
        second_request_messages = provider.requests[1]["messages"]
        tool_messages = [
            message
            for message in second_request_messages
            if message.get("role") == "tool"
        ]
        budget_user_messages = [
            message
            for message in second_request_messages
            if message.get("metadata", {}).get("kind") == "web_search_budget"
        ]
        self.assertEqual(1, len(tool_messages))
        self.assertEqual([], budget_user_messages)
        self.assertIn("remaining=\"5\"", tool_messages[0]["content"])
        self.assertIn("5 web_search calls remaining", tool_messages[0]["content"])
        self.assertIn("search result", tool_messages[0]["content"])

    def test_web_search_budget_denies_seventh_search_call(self) -> None:
        registry = _registry_with_tool(
            "web_search",
            enabled_modes={"bot"},
            always_on=True,
            handler=lambda **kwargs: "search result",
        )
        provider = ScriptedProvider([
            *[
                _tool_response(index, "web_search", {"query": str(index)})
                for index in range(1, 8)
            ],
            _final_response("done"),
        ])
        pipeline = _pipeline(registry, provider, max_reasoning_steps=10)
        session = Session(id="web:test", current_mode="bot")

        reply = pipeline.run(session, SimpleNamespace(tool_mode="bot"))

        self.assertEqual("done", reply)
        self.assertEqual(8, provider.calls)
        denied = [
            message
            for message in session.messages
            if message.get("role") == "tool"
            and message.get("status") == "denied"
            and "web_search budget exhausted" in str(message.get("content"))
        ]
        self.assertEqual(1, len(denied))
        self.assertEqual(0, session.metadata["web_search_budget_remaining"])

    def test_registry_distinguishes_hidden_and_forbidden_tools(self) -> None:
        registry = _registry_with_tool("git_add", enabled_modes={"coding"})
        session = Session(id="web:test", current_mode="bot")

        hidden = registry.execution_error_for_turn("git_add", session=session, mode="coding")
        forbidden = registry.execution_error_for_turn("git_add", session=session, mode="bot")
        unknown = registry.execution_error_for_turn("imaginary", session=session, mode="bot")

        self.assertIn("not visible in this turn", hidden)
        self.assertEqual("Tool 'git_add' is not allowed in bot mode.", forbidden)
        self.assertEqual("Unknown tool: imaginary", unknown)


if __name__ == "__main__":
    unittest.main()
