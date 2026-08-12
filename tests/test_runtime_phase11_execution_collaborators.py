from runtime.execution.message_sanitizer import sanitize_context_messages
from runtime.execution.model_invocation import invoke_model, supports_streaming
from tests.fakes.scripted_model import FinalResponse, ScriptedModel


def test_message_sanitizer_converts_orphan_tool_result():
    messages, dropped = sanitize_context_messages([
        {"role": "user", "content": "hello"},
        {"role": "tool", "tool_call_id": "missing", "content": "result"},
    ])

    assert [item["role"] for item in messages] == ["user", "user"]
    assert dropped == [{
        "index": 1,
        "role": "tool",
        "reason": "orphan_tool_result",
        "tool_call_id": "missing",
    }]


def test_model_invocation_preserves_non_streaming_call_contract():
    provider = ScriptedModel([FinalResponse("done")])

    response = invoke_model(
        provider,
        model="fake",
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        max_tokens=64,
    )

    assert response.content == "done"
    assert supports_streaming(provider, None) is False
