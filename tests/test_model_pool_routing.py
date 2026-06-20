import json
import unittest
from types import SimpleNamespace

from models.model_pool import ModelPool, ModelProfile, build_model_pool_from_env
from runtime.pipeline import Pipeline
from models.provider import LLMResponse, OpenAICompatibleProvider
from sessions.session import Session
from tools.executor import ToolExecutor


class ModelPoolEnvTests(unittest.TestCase):
    def test_builds_provider_pool_and_routes_from_env(self) -> None:
        env = {
            "LLM_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "deepseek-key",
            "MIMO_API_KEY": "mimo-key",
            "LLM_ROUTE_CHAT": "mimo,deepseek",
            "LLM_ROUTE_CODING": "deepseek",
            "LLM_ROUTE_SUMMARY": "mimo",
        }

        pool = build_model_pool_from_env(env)

        self.assertEqual("mimo", pool.primary_profile_name("chat"))
        self.assertEqual("mimo-v2.5-pro", pool.model_for("chat"))
        self.assertEqual("max_completion_tokens", pool.profile_for("chat").max_tokens_param)
        self.assertEqual(["deepseek"], pool.route_profile_names("coding"))
        self.assertEqual(["mimo"], pool.route_profile_names("summary"))
        self.assertEqual(["mimo"], pool.route_profile_names("reflection"))

    def test_provider_json_supports_profile_fallbacks(self) -> None:
        env = {
            "LLM_PROVIDER": "deepseek",
            "LLM_PROVIDERS_JSON": json.dumps({
                "deepseek": {
                    "api_key": "deepseek-key",
                    "base_url": "https://deepseek.example/v1",
                    "model": "deepseek-test",
                },
                "mimo": {
                    "api_key": "mimo-key",
                    "base_url": "https://mimo.example/v1",
                    "model": "mimo-test",
                    "max_tokens_param": "max_completion_tokens",
                    "fallbacks": ["deepseek"],
                },
            }),
            "LLM_ROUTE_CHAT": "mimo",
        }

        pool = build_model_pool_from_env(env)

        self.assertEqual(["mimo", "deepseek"], pool.route_profile_names("chat"))
        self.assertEqual("mimo-test", pool.model_for("chat"))

    def test_profile_supports_responses_wire_api_from_env(self) -> None:
        env = {
            "LLM_PROVIDER": "openai_relay",
            "OPENAI_RELAY_API_KEY": "relay-key",
            "OPENAI_RELAY_BASE_URL": "http://relay.example",
            "OPENAI_RELAY_MODEL": "gpt-test",
            "OPENAI_RELAY_WIRE_API": "responses",
        }

        pool = build_model_pool_from_env(env)

        self.assertEqual("responses", pool.profile_for("chat").wire_api)

    def test_gemini_provider_uses_openai_compatibility_defaults(self) -> None:
        env = {
            "LLM_PROVIDER": "gemini",
            "GEMINI_API_KEY": "gemini-key",
        }

        pool = build_model_pool_from_env(env)

        self.assertEqual("gemini", pool.primary_profile_name("chat"))
        self.assertEqual("gemini", pool.profile_for("chat").provider)
        self.assertEqual(
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            pool.profile_for("chat").base_url,
        )
        self.assertEqual("gemini-3.5-flash", pool.model_for("chat"))

    def test_provider_json_supports_responses_wire_api(self) -> None:
        env = {
            "LLM_PROVIDER": "relay",
            "LLM_PROVIDERS_JSON": json.dumps({
                "relay": {
                    "api_key": "relay-key",
                    "base_url": "http://relay.example",
                    "model": "gpt-test",
                    "wire_api": "responses",
                },
            }),
        }

        pool = build_model_pool_from_env(env)

        self.assertEqual("responses", pool.profile_for("chat").wire_api)


class RoutedModelProviderTests(unittest.TestCase):
    def test_chat_falls_back_to_next_provider(self) -> None:
        pool = ModelPool(
            profiles={
                "primary": ModelProfile(
                    name="primary",
                    provider="custom",
                    api_key="p",
                    base_url="https://primary.example",
                    model="primary-model",
                ),
                "backup": ModelProfile(
                    name="backup",
                    provider="custom",
                    api_key="b",
                    base_url="https://backup.example",
                    model="backup-model",
                ),
            },
            routes={"chat": ("primary", "backup")},
            default_profile="primary",
        )
        primary = FailingProvider()
        backup = RecordingProvider("ok")
        pool._providers["primary"] = primary
        pool._providers["backup"] = backup

        response = pool.routed_provider("chat").chat(
            model="override-primary-model",
            messages=[],
            tools=[],
            tool_choice="none",
            max_tokens=100,
        )

        self.assertEqual("ok", response.content)
        self.assertEqual(1, primary.calls)
        self.assertEqual("backup-model", backup.calls[0]["model"])

    def test_health_check_failure_switches_active_model(self) -> None:
        pool = ModelPool(
            profiles={
                "primary": ModelProfile(
                    name="primary",
                    provider="custom",
                    api_key="p",
                    base_url="https://primary.example",
                    model="primary-model",
                ),
                "backup": ModelProfile(
                    name="backup",
                    provider="custom",
                    api_key="b",
                    base_url="https://backup.example",
                    model="backup-model",
                ),
            },
            routes={"chat": ("primary", "backup")},
            default_profile="primary",
            failure_threshold=1,
            failure_cooldown_seconds=60,
        )
        pool._providers["primary"] = FailingProvider()
        pool._providers["backup"] = RecordingProvider("ok")

        result = pool.health_check_profile("primary")

        self.assertEqual("failed", result["status"])
        self.assertFalse(pool.profile_available("primary"))
        self.assertEqual("backup-model", pool.model_for("chat"))

    def test_unhealthy_primary_is_skipped_until_cooldown_expires(self) -> None:
        pool = ModelPool(
            profiles={
                "primary": ModelProfile(
                    name="primary",
                    provider="custom",
                    api_key="p",
                    base_url="https://primary.example",
                    model="primary-model",
                ),
                "backup": ModelProfile(
                    name="backup",
                    provider="custom",
                    api_key="b",
                    base_url="https://backup.example",
                    model="backup-model",
                ),
            },
            routes={"chat": ("primary", "backup")},
            default_profile="primary",
            failure_threshold=1,
            failure_cooldown_seconds=60,
        )
        primary = FailingProvider()
        backup = RecordingProvider("ok")
        pool._providers["primary"] = primary
        pool._providers["backup"] = backup

        first = pool.routed_provider("chat").chat(
            model="primary-model",
            messages=[],
            tools=[],
            tool_choice="none",
            max_tokens=100,
        )
        second = pool.routed_provider("chat").chat(
            model="primary-model",
            messages=[],
            tools=[],
            tool_choice="none",
            max_tokens=100,
        )

        self.assertEqual("ok", first.content)
        self.assertEqual("ok", second.content)
        self.assertEqual(1, primary.calls)
        self.assertEqual(2, len(backup.calls))
        self.assertEqual("backup-model", backup.calls[1]["model"])

    def test_stream_fallback_only_before_text_is_emitted(self) -> None:
        pool = ModelPool(
            profiles={
                "primary": ModelProfile(
                    name="primary",
                    provider="custom",
                    api_key="p",
                    base_url="https://primary.example",
                    model="primary-model",
                ),
                "backup": ModelProfile(
                    name="backup",
                    provider="custom",
                    api_key="b",
                    base_url="https://backup.example",
                    model="backup-model",
                ),
            },
            routes={"chat": ("primary", "backup")},
            default_profile="primary",
        )
        pool._providers["primary"] = StreamingFailingProvider(emit_first=False)
        pool._providers["backup"] = StreamingProvider("backup reply")
        emitted = []

        response = pool.routed_provider("chat").stream_chat(
            model="primary-model",
            messages=[],
            tools=[],
            tool_choice="none",
            max_tokens=100,
            on_text=emitted.append,
        )

        self.assertEqual("backup reply", response.content)
        self.assertEqual(["backup reply"], emitted)

    def test_stream_does_not_fallback_after_text_is_emitted(self) -> None:
        pool = ModelPool(
            profiles={
                "primary": ModelProfile(
                    name="primary",
                    provider="custom",
                    api_key="p",
                    base_url="https://primary.example",
                    model="primary-model",
                ),
                "backup": ModelProfile(
                    name="backup",
                    provider="custom",
                    api_key="b",
                    base_url="https://backup.example",
                    model="backup-model",
                ),
            },
            routes={"chat": ("primary", "backup")},
            default_profile="primary",
        )
        pool._providers["primary"] = StreamingFailingProvider(emit_first=True)
        pool._providers["backup"] = StreamingProvider("backup reply")
        emitted = []

        with self.assertRaisesRegex(RuntimeError, "stream down"):
            pool.routed_provider("chat").stream_chat(
                model="primary-model",
                messages=[],
                tools=[],
                tool_choice="none",
                max_tokens=100,
                on_text=emitted.append,
            )

        self.assertEqual(["partial"], emitted)
        self.assertEqual(0, pool._providers["backup"].calls)


class PipelineModelRoutingTests(unittest.TestCase):
    def test_pipeline_uses_coding_route_for_coding_profile(self) -> None:
        model_pool = FakeModelPool()
        pipeline = _pipeline_with_pool(model_pool)
        session = Session(id="task:test", current_mode="coding")
        session.add_message("user", "edit code")

        reply = pipeline.run(session, SimpleNamespace(tool_mode="coding"))

        self.assertEqual("reply from coding", reply)
        self.assertEqual("coding-model", model_pool.providers["coding"].calls[0]["model"])

class RecordingProvider:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(
            content=self.content,
            raw_message={"role": "assistant", "content": self.content},
        )


class FailingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, **kwargs):
        self.calls += 1
        raise RuntimeError("provider down")


class StreamingProvider:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    def stream_chat(self, **kwargs):
        self.calls += 1
        kwargs["on_text"](self.content)
        return LLMResponse(
            content=self.content,
            raw_message={"role": "assistant", "content": self.content},
        )


class StreamingFailingProvider:
    def __init__(self, *, emit_first: bool) -> None:
        self.emit_first = emit_first
        self.calls = 0

    def stream_chat(self, **kwargs):
        self.calls += 1
        if self.emit_first:
            kwargs["on_text"]("partial")
        raise RuntimeError("stream down")


class FakeTools:
    def reset_turn_unlocks(self, session) -> None:
        return None

    def schemas_for_turn(self, session, mode):
        return []


class FakeContextBuilder:
    def build(self, **kwargs):
        return SimpleNamespace(messages=kwargs["session"].messages)


class FakeModelPool:
    def __init__(self) -> None:
        self.providers = {}

    def routed_provider(self, purpose: str):
        self.providers.setdefault(purpose, RecordingProvider(f"reply from {purpose}"))
        return self.providers[purpose]

    def model_for(self, purpose: str) -> str:
        return f"{purpose}-model"


def _pipeline_with_pool(model_pool) -> Pipeline:
    return Pipeline(
        tools=FakeTools(),
        provider=RecordingProvider("default"),
        model="default-model",
        model_pool=model_pool,
        tool_executor=ToolExecutor([]),
        context_builder=FakeContextBuilder(),
    )


class ChatCompletionsProviderTests(unittest.TestCase):
    def test_chat_provider_drops_empty_assistant_messages(self) -> None:
        client = FakeChatClient("ok")
        provider = OpenAICompatibleProvider(client)

        response = provider.chat(
            model="chat-test",
            messages=[
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": None},
                {"role": "assistant", "content": ""},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_prev",
                    "type": "function",
                    "function": {"name": "old_tool", "arguments": "{}"},
                }]},
                {"role": "tool", "tool_call_id": "call_prev", "content": "done"},
                {"role": "user", "content": "next"},
            ],
            tools=[],
            tool_choice="none",
            max_tokens=20,
        )

        sent_messages = client.chat.completions.calls[0]["messages"]
        self.assertEqual("ok", response.content)
        self.assertNotIn({"role": "assistant", "content": None}, sent_messages)
        self.assertNotIn({"role": "assistant", "content": ""}, sent_messages)
        self.assertEqual("old_tool", sent_messages[1]["tool_calls"][0]["function"]["name"])


class ResponsesProviderTests(unittest.TestCase):
    def test_responses_provider_extracts_text(self) -> None:
        client = FakeResponsesClient(FakeResponse(
            output=[
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "hello"},
                    ],
                },
            ],
        ))
        provider = OpenAICompatibleProvider(client, wire_api="responses")

        response = provider.chat(
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            tool_choice="none",
            max_tokens=20,
        )

        self.assertEqual("hello", response.content)
        self.assertEqual("gpt-test", client.responses.calls[0]["model"])
        self.assertEqual(20, client.responses.calls[0]["max_output_tokens"])
        self.assertEqual([{"role": "user", "content": "hi"}], client.responses.calls[0]["input"])

    def test_responses_provider_converts_tools_and_tool_calls(self) -> None:
        client = FakeResponsesClient(FakeResponse(
            output=[
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read_file",
                    "arguments": '{"path": "README.md"}',
                },
            ],
        ))
        provider = OpenAICompatibleProvider(client, wire_api="responses")

        response = provider.chat(
            model="gpt-test",
            messages=[
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_prev",
                        "type": "function",
                        "function": {
                            "name": "old_tool",
                            "arguments": "{}",
                        },
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_prev",
                    "content": "done",
                },
                {"role": "user", "content": "read"},
            ],
            tools=[{
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
            tool_choice="auto",
            max_tokens=20,
        )

        self.assertEqual("read_file", response.tool_calls[0].name)
        self.assertEqual({"path": "README.md"}, response.tool_calls[0].arguments)
        self.assertEqual("read_file", client.responses.calls[0]["tools"][0]["name"])
        self.assertIn("tool_calls", response.raw_message)


class FakeResponse:
    def __init__(self, *, output, output_text: str | None = None) -> None:
        self.output = output
        if output_text is not None:
            self.output_text = output_text


class FakeResponsesEndpoint:
    def __init__(self, response) -> None:
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeResponsesClient:
    def __init__(self, response) -> None:
        self.responses = FakeResponsesEndpoint(response)


class FakeChatMessage:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = []

    def model_dump(self, exclude_none: bool = False):
        return {"role": "assistant", "content": self.content}


class FakeChatChoice:
    def __init__(self, content: str) -> None:
        self.message = FakeChatMessage(content)


class FakeChatResponse:
    def __init__(self, content: str) -> None:
        self.choices = [FakeChatChoice(content)]


class FakeChatCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeChatResponse(self.content)


class FakeChatClient:
    def __init__(self, content: str) -> None:
        self.chat = SimpleNamespace(
            completions=FakeChatCompletions(content),
        )


if __name__ == "__main__":
    unittest.main()
