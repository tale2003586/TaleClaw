import json
import os
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from models.model_pool import build_model_pool_from_env
from models.provider import OpenAICompatibleProvider
from runtime.agent_spec import AgentSpec
from runtime.execution.state import RunExecutionState
from web.server import RequestHandler, _configured_model_options, _thinking_supported, _validated_model_profile


class RecordingCompletions:
    def __init__(self):
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if kwargs.get("stream"):
            return []
        message = SimpleNamespace(
            content="ok",
            tool_calls=[],
            model_dump=lambda **_: {"role": "assistant", "content": "ok"},
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def provider(*, supports=False, param="", value=True):
    completions = RecordingCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return OpenAICompatibleProvider(
        client,
        supports_thinking=supports,
        thinking_param=param,
        thinking_value=value,
    ), completions


class ThinkingCapabilityTests(unittest.TestCase):
    def test_health_model_options_and_selected_thinking_capability(self):
        env = {
            "LLM_PROVIDER": "relay",
            "LLM_PROVIDERS_JSON": json.dumps({
                "relay": {
                    "api_key": "test",
                    "base_url": "https://relay.invalid/v1",
                    "model": "reasoning-model",
                    "supports_thinking": True,
                    "thinking_param": "reasoning_effort",
                },
                "plain": {
                    "api_key": "test",
                    "base_url": "https://plain.invalid/v1",
                    "model": "plain-model",
                },
            }),
        }
        with patch.dict(os.environ, env, clear=True):
            options, default_profile = _configured_model_options()

            self.assertEqual("relay", default_profile)
            self.assertEqual({"relay", "plain"}, {item["profile"] for item in options})
            self.assertTrue(_thinking_supported("relay"))
            self.assertFalse(_thinking_supported("plain"))
            self.assertEqual("plain", _validated_model_profile("plain"))
            with self.assertRaisesRegex(ValueError, "not configured"):
                _validated_model_profile("missing")

    def test_default_is_false(self):
        self.assertFalse(AgentSpec(name="chat").thinking_enabled)
        self.assertFalse(RunExecutionState().thinking_enabled)

    def test_unsupported_provider_does_not_receive_unknown_parameter(self):
        adapter, calls = provider()
        adapter.chat(
            model="model", messages=[], tools=[], max_tokens=20,
            thinking_enabled=True,
        )
        self.assertNotIn("thinking", calls.requests[0])

    def test_supported_provider_maps_true_for_plain_and_tool_requests(self):
        adapter, calls = provider(supports=True, param="thinking")
        adapter.chat(
            model="model", messages=[], tools=[], max_tokens=20,
            thinking_enabled=True,
        )
        adapter.chat(
            model="model", messages=[], tools=[{"type": "function", "function": {}}],
            max_tokens=20, thinking_enabled=True,
        )
        self.assertTrue(calls.requests[0]["thinking"])
        self.assertTrue(calls.requests[1]["thinking"])
        self.assertIn("tools", calls.requests[1])

    def test_streaming_uses_the_same_capability_mapping(self):
        adapter, calls = provider(supports=True, param="thinking")
        adapter.stream_chat(
            model="model", messages=[], tools=[], max_tokens=20,
            thinking_enabled=True, on_text=lambda _: None,
        )
        self.assertTrue(calls.requests[0]["thinking"])
        self.assertTrue(calls.requests[0]["stream"])

    def test_provider_specific_thinking_value_is_forwarded(self):
        adapter, calls = provider(
            supports=True,
            param="reasoning_effort",
            value="high",
        )

        adapter.chat(
            model="model", messages=[], tools=[], max_tokens=20,
            thinking_enabled=True,
        )

        self.assertEqual("high", calls.requests[0]["reasoning_effort"])

    def test_support_without_parameter_is_rejected(self):
        profiles = {
            "relay": {
                "provider": "relay",
                "api_key": "test",
                "base_url": "https://relay.invalid/v1",
                "model": "model",
                "supports_thinking": True,
            }
        }
        with self.assertRaisesRegex(RuntimeError, "requires thinking_param"):
            build_model_pool_from_env({
                "LLM_PROVIDER": "relay",
                "LLM_PROVIDERS_JSON": json.dumps(profiles),
            })

    def test_non_streaming_web_api_rejects_unsupported_true_with_400(self):
        handler = object.__new__(RequestHandler)
        handler._read_json_body = lambda: {
            "session_id": "default",
            "message": "hello",
            "thinking_enabled": True,
        }
        responses = []
        handler._send_json = lambda payload, status=HTTPStatus.OK: responses.append((payload, status))
        with patch("web.server._thinking_supported", return_value=False):
            handler._handle_chat()
        self.assertEqual(HTTPStatus.BAD_REQUEST, responses[0][1])
        self.assertIn("unavailable", responses[0][0]["error"])

    def test_ui_disables_control_from_backend_capability(self):
        source = Path("web/frontend/src/pages/ChatPage.tsx").read_text(encoding="utf-8")
        self.assertIn("automaticModel?.supports_thinking", source)
        self.assertIn("disabled={!thinkingAvailable", source)
        self.assertIn("thinkingEnabled", source)


if __name__ == "__main__":
    unittest.main()
