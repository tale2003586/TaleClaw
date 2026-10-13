"""Responses must preserve optional tool arguments across every request path."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS

import pytest

from models.provider import OpenAICompatibleProvider, _tools_to_responses_tools
from tools.schema import LEAD_TOOLS, UPDATE_TASK_STATE_TOOL
from tests.test_native_compaction_regressions import Client as NativeClient, USER, response
from tests.test_provider_contract import Client, Stream, chat_response, invoke


def actual_tools():
    artifact = next(tool for tool in LEAD_TOOLS if tool.get("function", {}).get("name") == "read_artifact")
    return deepcopy([artifact, UPDATE_TASK_STATE_TOOL])


def test_real_optional_tool_parameters_remain_optional_and_nonnullable():
    tools = actual_tools()
    original = deepcopy(tools)
    converted = _tools_to_responses_tools(tools)
    for before, after in zip(original, converted):
        assert after["strict"] is False
        assert after["parameters"] == before["function"]["parameters"]
    artifact = converted[0]["parameters"]
    assert artifact["required"] == ["artifact_ref"]
    assert artifact["properties"]["start_seq"]["type"] == "integer"
    assert artifact["properties"]["end_seq"]["type"] == "integer"
    state = converted[1]["parameters"]
    assert "hypotheses_add" not in state["required"]
    assert "evidence_add" not in state["required"]
    # The caller's tool registry remains unchanged even if a transport mutates
    # its independent serialization object.
    converted[0]["parameters"]["required"].append("query")
    assert tools == original


@pytest.mark.parametrize("strict", [True, False, None])
@pytest.mark.parametrize("shape", ["chat", "responses"])
def test_explicit_strictness_is_preserved_in_nested_and_flat_function_tools(strict, shape):
    nested = actual_tools()[0]
    nested["function"]["strict"] = strict
    tool = nested if shape == "chat" else {"type": "function", **nested["function"], "defer_loading": True}
    converted = _tools_to_responses_tools([tool])[0]
    assert converted["strict"] is (False if strict is None else strict)
    assert converted["name"] == "read_artifact"
    assert converted["parameters"] == nested["function"]["parameters"]
    if shape == "responses":
        assert converted["defer_loading"] is True


@pytest.mark.parametrize("invalid", ["false", "true", 1, 0, [], {}])
def test_nonboolean_strictness_is_not_silently_coerced(invalid):
    tool = actual_tools()[0]
    tool["function"]["strict"] = invalid
    with pytest.raises(ValueError, match="strict"):
        _tools_to_responses_tools([tool])


def test_nonfunction_native_tools_are_preserved():
    tools = [{"type": "web_search"}, {"type": "code_interpreter", "container": {"type": "auto"}}]
    assert _tools_to_responses_tools(tools) == tools


@pytest.mark.parametrize("mode", ["responses", "responses_stream", "native", "native_stream", "count"])
def test_regular_stream_native_and_token_count_share_optional_semantics(mode):
    tools = actual_tools()
    original = deepcopy(tools)
    stream = "stream" in mode
    result = Stream([NS(type="response.completed", response=response())]) if stream else response()
    client = NativeClient([result])
    provider = OpenAICompatibleProvider(client, wire_api="responses", context_window_tokens=10000)
    if mode == "count":
        assert provider.count_native_input(input_items=[USER], model="fixture", tools=tools) == 50
    elif mode.startswith("native"):
        provider.native_chat(input_items=[USER], tools=tools, model="fixture", max_tokens=20,
                             on_text=(lambda _: None) if stream else None)
    else:
        invoke(provider, stream=stream, tools=tools)
    calls = client.create_calls + client.count_calls
    assert calls
    for call in calls:
        assert [tool["strict"] for tool in call["tools"]] == [False, False]
        assert [tool["parameters"] for tool in call["tools"]] == [tool["function"]["parameters"] for tool in original]
    assert tools == original


def test_omitted_event_filters_are_not_filled_or_rewritten_in_model_arguments():
    arguments = {"artifact_ref": "event://evt-original", "limit": 2000}
    returned = {"type": "function_call", "id": "fc1", "call_id": "c1", "name": "read_artifact",
                "arguments": json.dumps(arguments), "status": "completed"}
    client = NativeClient([response([returned])])
    provider = OpenAICompatibleProvider(client, wire_api="responses")
    result = invoke(provider, tools=actual_tools())
    assert result.tool_calls[0].arguments == arguments
    assert "start_seq" not in result.tool_calls[0].arguments
    assert "end_seq" not in result.tool_calls[0].arguments


def test_chat_completions_keeps_its_existing_tool_contract():
    tools = actual_tools()
    client = Client(chat_response())
    invoke(OpenAICompatibleProvider(client), tools=tools)
    sent = client.endpoint.calls[0]["tools"]
    assert sent == tools
    assert all("strict" not in tool["function"] for tool in sent)
