"""Native Responses state contracts. Every request uses an in-process fake."""
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from models.context_state import (
    NATIVE_CONTEXT_KEY, NativeContextConflict, NativeContextLineage,
    commit_native_context, load_native_context, prepare_native_context,
)
from models.provider import (
    ModelProtocolError, NativeContextUnsupported, OpenAICompatibleProvider,
    messages_to_native_input,
)
from runtime.context.dynamic_budget import PromptBudgetExceeded
from runtime.context.native_policy import invoke_native_policy
from runtime.context.policies import ContextPolicy
from runtime.execution.budget import ExecutionBudget, ExecutionCancelled, execution_scope
from runtime.sessions.session import Session
from tests.test_provider_contract import Stream


OPAQUE = {"type": "compaction", "id": "cmp-1", "encrypted_content": "opaque-secret-ciphertext", "future_field": {"retained": True}}
REASONING = {"type": "reasoning", "id": "rs-1", "summary": [], "encrypted_content": "opaque-reasoning"}
MESSAGE = {"type": "message", "id": "msg-1", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "Continue tracing", "annotations": []}]}
CALL = {"type": "function_call", "id": "fc-1", "call_id": "call-1", "name": "inspect", "arguments": "{}", "status": "completed"}
USER = {"role": "user", "content": "debug parser"}


def response(output=None):
    return {"id": "resp-1", "status": "completed", "output": deepcopy([MESSAGE] if output is None else output),
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                      "input_tokens_details": {"cached_tokens": 3}}}


def compacted(output=None):
    return {"id": "cmp-response", "object": "response.compaction", "output": deepcopy([USER, OPAQUE] if output is None else output),
            "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}}


class Client:
    def __init__(self, responses, *, compactions=(), count=50):
        self.base_url = "https://api.openai.com/v1"
        self.responses = self
        self.input_tokens = NS(count=self.count)
        self.results = iter(responses)
        self.compactions = iter(compactions)
        self.count_result = count
        self.create_calls, self.compact_calls, self.count_calls = [], [], []
        self.options = []

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self

    def create(self, **kwargs):
        self.create_calls.append(deepcopy(kwargs))
        value = next(self.results)
        if isinstance(value, Exception):
            raise value
        return value() if callable(value) else value

    def compact(self, **kwargs):
        self.compact_calls.append(deepcopy(kwargs))
        value = next(self.compactions)
        if isinstance(value, Exception):
            raise value
        return value

    def count(self, **kwargs):
        self.count_calls.append(deepcopy(kwargs))
        if isinstance(self.count_result, Exception):
            raise self.count_result
        return {"input_tokens": self.count_result, "object": "response.input_tokens"}


def provider(client):
    return OpenAICompatibleProvider(client, wire_api="responses", context_window_tokens=10_000)


def invoke(session, model_provider, *, model="fixture", policy=None, messages=None, on_text=None, hard_limit=8_000):
    return invoke_native_policy(
        session=session, provider=model_provider, model=model,
        messages=messages if messages is not None else [{"role": "system", "content": "Current task facts"}, *session.messages],
        tools=[], max_tokens=200, on_text=on_text, thinking_enabled=False,
        hard_limit=hard_limit, policy=policy or ContextPolicy(name="native"),
    )


def seeded():
    session = Session("native:session")
    session.add_message(**USER)
    return session


def test_standalone_canonical_window_retains_every_item_without_textualizing():
    window = [USER, CALL, {"type": "function_call_output", "call_id": "call-1", "output": "file content"}, REASONING, OPAQUE]
    client = Client([response()], compactions=[compacted(window)])
    session = seeded()
    session.metadata["context_compaction_request"] = {"status": "pending", "message_count": 1, "focus": "parser"}
    budget = ExecutionBudget()
    with execution_scope(budget=budget):
        result = invoke(session, provider(client), policy=ContextPolicy(name="native", native_mode="standalone"))
    assert client.create_calls[0]["input"] == window
    assert session.metadata[NATIVE_CONTEXT_KEY]["input_items"] == [*window, MESSAGE]
    assert "Current task facts" in client.create_calls[0]["instructions"]
    assert "max_output_tokens" not in client.compact_calls[0]
    assert result.provider_metadata["compaction_usage"]["total_tokens"] == 13
    assert session.metadata["context_compaction_request"]["status"] == "completed"
    assert budget.model_attempts == 4  # two exact counts, compact, generate
    assert budget.known_tokens == 28
    assert budget.unknown_usage_attempts == 2  # counts are not billed-generation usage


def test_server_output_is_appended_losslessly_and_second_turn_uses_only_raw_delta():
    client = Client([response([REASONING, OPAQUE, MESSAGE, CALL]), response()])
    session = seeded()
    first = invoke(session, provider(client), messages=[{"role": "system", "content": "VERSION ONE"}, *session.messages])
    session.messages.append(first.raw_message)
    session.add_message("tool", "original tool result", tool_call_id="call-1")
    # The prompt projection intentionally lacks the raw tool result.
    current = [{"role": "system", "content": "VERSION TWO"}, {"role": "user", "content": "lookup clue", "metadata": {"origin": "retrieval", "instructions": False}}]
    invoke(session, provider(client), messages=current)
    expected = [USER, REASONING, OPAQUE, MESSAGE, CALL,
                {"type": "function_call_output", "call_id": "call-1", "output": "original tool result"}]
    assert client.create_calls[1]["input"] == expected
    assert "VERSION TWO" in client.create_calls[1]["instructions"]
    assert "lookup clue" in client.create_calls[1]["instructions"]
    assert "VERSION ONE" not in client.create_calls[1]["instructions"]
    assert client.create_calls[0]["context_management"][0]["type"] == "compaction"
    assert client.create_calls[0]["store"] is False
    assert client.create_calls[0]["include"] == ["reasoning.encrypted_content"]
    assert client.create_calls[0]["truncation"] == "disabled"


def test_linear_adapter_preserves_unknown_native_fields_and_function_interactions():
    ordinary = [{"role": "assistant", "content": "inspect", "tool_calls": [{"id": "call-1", "function": {"name": "inspect", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call-1", "content": "raw output"}]
    converted = messages_to_native_input([OPAQUE, *ordinary])
    assert converted[0] == OPAQUE
    assert converted[-1] == {"type": "function_call_output", "call_id": "call-1", "output": "raw output"}
    assert converted[2]["type"] == "function_call"
    echoed = {"role": "assistant", "content": "must not duplicate", "metadata": {"native_context": {"output_items": [REASONING, MESSAGE]}}}
    assert messages_to_native_input([echoed]) == [REASONING, MESSAGE]


def test_streamed_compaction_is_committed_only_after_completed_final_output():
    final = response([OPAQUE, MESSAGE])
    stream = Stream([NS(type="response.output_item.done", output_index=0, item=OPAQUE), NS(type="response.completed", response=final)])
    client = Client([stream])
    session = seeded()
    invoke(session, provider(client), on_text=lambda _: None)
    assert session.metadata[NATIVE_CONTEXT_KEY]["input_items"] == [USER, OPAQUE, MESSAGE]
    assert stream.closed


def test_streamed_compaction_disagreement_with_completed_output_rejects_state():
    stream = Stream([NS(type="response.output_item.done", output_index=0, item=OPAQUE), NS(type="response.completed", response=response())])
    session = seeded()
    with pytest.raises(ModelProtocolError, match="native item conflicts"):
        invoke(session, provider(Client([stream])), on_text=lambda _: None)
    assert NATIVE_CONTEXT_KEY not in session.metadata
    assert stream.closed


def test_stream_cancellation_keeps_previous_window_untouched_and_counts_attempts():
    session = seeded()
    initial_client = Client([response()])
    first = invoke(session, provider(initial_client))
    session.messages.append(first.raw_message)
    before = deepcopy(session.metadata[NATIVE_CONTEXT_KEY])
    cancelled = False
    def on_text(_):
        nonlocal cancelled
        cancelled = True
    stream = Stream([NS(type="response.output_text.delta", delta="partial"), NS(type="response.completed", response=response([OPAQUE, MESSAGE]))])
    budget = ExecutionBudget(cancel_requested=lambda: cancelled)
    with execution_scope(budget=budget), pytest.raises(ExecutionCancelled):
        invoke(session, provider(Client([stream])), on_text=on_text)
    assert session.metadata[NATIVE_CONTEXT_KEY] == before
    assert stream.closed
    assert budget.model_attempts == 2
    assert budget.unknown_usage_attempts == 2


def test_compact_success_followed_by_generation_error_rolls_back_entire_window():
    session = seeded()
    client = Client([response(), RuntimeError("generation unavailable")], compactions=[compacted()])
    native_provider = provider(client)
    first = invoke(session, native_provider)
    session.messages.append(first.raw_message)
    session.add_message("user", "continue")
    before = deepcopy(session.metadata[NATIVE_CONTEXT_KEY])
    session.metadata["context_compaction_request"] = {"status": "pending", "message_count": 3}
    with pytest.raises(RuntimeError, match="generation unavailable"):
        invoke(session, native_provider)
    assert session.metadata[NATIVE_CONTEXT_KEY] == before
    assert session.metadata["context_compaction_request"]["status"] == "failed"


@pytest.mark.parametrize("changed", ["model", "workspace", "endpoint", "session"])
def test_lineage_changes_cannot_reuse_other_lane_opaque_state(changed):
    session = seeded()
    client = Client([response([OPAQUE, MESSAGE]), response()])
    native_provider = provider(client)
    first = invoke(session, native_provider)
    session.messages.append(first.raw_message)
    model = "fixture"
    if changed == "model":
        model = "other-model"
    elif changed == "workspace":
        session.metadata["workspace_root"] = "/tmp/other-workspace"
    elif changed == "endpoint":
        client.base_url = "https://other.example/v1"
        native_provider.native_compaction_supported = True
    else:
        other = Session("native:other", messages=deepcopy(session.messages), metadata=deepcopy(session.metadata))
        session = other
    invoke(session, native_provider, model=model)
    assert OPAQUE not in client.create_calls[1]["input"]
    assert client.create_calls[1]["input"][0] == USER


def test_unknown_compatible_endpoint_needs_explicit_support_and_counting():
    client = Client([response()])
    client.base_url = "https://unknown.example/v1"
    with pytest.raises(NativeContextUnsupported):
        invoke(seeded(), provider(client))
    assert not client.count_calls
    assert not client.create_calls
    declared = OpenAICompatibleProvider(client, wire_api="responses", native_compaction_supported=True)
    assert invoke(seeded(), declared).content == "Continue tracing"


def test_native_input_count_failure_or_overflow_never_reaches_generation():
    for count, error in [(True, ModelProtocolError), (10_000, PromptBudgetExceeded), (RuntimeError("count offline"), RuntimeError)]:
        session = seeded()
        client = Client([response()], count=count)
        budget = ExecutionBudget()
        with execution_scope(budget=budget), pytest.raises(error):
            invoke(session, provider(client), hard_limit=100)
        assert not client.create_calls
        assert NATIVE_CONTEXT_KEY not in session.metadata
        assert budget.model_attempts == 1
        assert budget.unknown_usage_attempts == 1


def test_ciphertext_is_counted_by_endpoint_not_its_character_length():
    opaque = {**OPAQUE, "encrypted_content": "x" * 200_000}
    client = Client([response()], count=50)
    result = provider(client).native_chat(input_items=[opaque], tools=[], model="fixture", max_tokens=200, hard_limit_tokens=100)
    assert result.content
    assert client.count_calls[0]["input"] == [opaque]
    assert client.create_calls[0]["input"] == [opaque]


def test_missing_expected_assistant_does_not_skip_the_next_user_message():
    session = seeded()
    client = Client([response(), response()])
    invoke(session, provider(client))
    # Simulate a host bug: it did not append the assistant turn it received.
    session.add_message("user", "A NEW REQUIRED CONSTRAINT")
    with pytest.raises(NativeContextConflict, match="expected native assistant"):
        invoke(session, provider(client))
    assert len(client.create_calls) == 1


def test_changed_source_prefix_and_concurrent_state_commit_are_rejected():
    session = seeded()
    client = Client([response(), response()])
    first = invoke(session, provider(client))
    session.messages.append(first.raw_message)
    session.messages[0]["content"] = "edited original request"
    with pytest.raises(NativeContextConflict, match="source history changed"):
        invoke(session, provider(client))
    lineage = NativeContextLineage(session.id, "fixture", "provider", "workspace")
    a = prepare_native_context(session, lineage, new_items=[USER], event_cursor=1)
    b = prepare_native_context(session, lineage, new_items=[USER], event_cursor=1)
    result = provider(Client([response()])).native_chat(input_items=a.input_items, tools=[], model="fixture", max_tokens=200)
    commit_native_context(session, a, result)
    with pytest.raises(NativeContextConflict, match="changed while"):
        commit_native_context(session, b, result)
    assert load_native_context(session, lineage).revision == 1


def test_runtime_source_guidance_is_fresh_control_context_not_durable_user_intent():
    session = seeded()
    session.add_message("user", "runtime reminder", metadata={"origin": "runtime", "kind": "runtime_retry"})
    client = Client([response()])
    invoke(session, provider(client), messages=[{"role": "system", "content": "task facts"}])
    assert client.create_calls[0]["input"] == [USER]
    assert "runtime reminder" in client.create_calls[0]["instructions"]


class RemoteError(RuntimeError):
    def __init__(self, status, *, code="", param=""):
        super().__init__("remote request failed")
        self.status_code, self.code, self.param = status, code, param


@pytest.mark.parametrize("operation", ["count", "compact", "server"])
def test_explicit_remote_unsupported_is_reported_without_mutating_state(operation):
    kwargs = {}
    results = [response()]
    if operation == "count":
        kwargs["count"] = RemoteError(404)
    elif operation == "compact":
        kwargs["compactions"] = [RemoteError(501)]
    else:
        results = [RemoteError(400, code="unsupported_parameter", param="context_management")]
    client = Client(results, **kwargs)
    session = seeded()
    if operation == "compact":
        session.metadata["context_compaction_request"] = {"status": "pending", "message_count": 1}
    with pytest.raises(NativeContextUnsupported):
        invoke(session, provider(client))
    assert NATIVE_CONTEXT_KEY not in session.metadata


def test_authentication_failure_is_not_mislabeled_as_native_unsupported():
    with pytest.raises(RemoteError):
        invoke(seeded(), provider(Client([response()], count=RemoteError(401))))


def test_json_persisted_window_resumes_with_exact_opaque_items_and_only_new_delta():
    import json
    client = Client([response([REASONING, OPAQUE, MESSAGE, CALL]), response()])
    session = seeded()
    first = invoke(session, provider(client))
    session.add_message(**first.raw_message)
    # Same JSON representation used by SessionStore; rebuild fresh Python objects
    # to ensure no client- or object-identity-only state is needed for continuation.
    restored = Session(**json.loads(json.dumps({
        "id": session.id, "messages": session.messages, "metadata": session.metadata,
        "event_log": [event.to_dict() for event in session.event_log],
    })))
    restored.add_message("tool", "after restart", tool_call_id="call-1")
    invoke(restored, provider(client))
    assert client.create_calls[1]["input"] == [USER, REASONING, OPAQUE, MESSAGE, CALL,
        {"type": "function_call_output", "call_id": "call-1", "output": "after restart"}]


def test_trace_contains_native_summary_without_ciphertext_or_mutating_response():
    from runtime.execution.reasoning_loop import _provider_metadata_for_trace
    result = provider(Client([response([OPAQUE, MESSAGE])])).native_chat(
        input_items=[USER], tools=[], model="fixture", max_tokens=200,
    )
    original = deepcopy(result.provider_metadata)
    trace = _provider_metadata_for_trace(result)
    assert "output_items" not in trace["native_context"]
    assert trace["native_context"]["output_item_count"] == 2
    assert trace["native_context"]["input_tokens_counted"] == 50
    assert "opaque-secret-ciphertext" not in str(trace)
    assert result.provider_metadata == original


def test_empty_compaction_ciphertext_is_a_protocol_error():
    session = seeded()
    client = Client([response([{**OPAQUE, "encrypted_content": ""}, MESSAGE])])
    with pytest.raises(ModelProtocolError, match="encrypted_content"):
        invoke(session, provider(client))
    assert NATIVE_CONTEXT_KEY not in session.metadata


@pytest.mark.parametrize("compressed", [False, True])
def test_native_artifact_visibility_requires_verbatim_output_after_opaque_boundary(compressed):
    from runtime.context.artifact_access import evidence_response_digest
    session = seeded()
    session.add_message("assistant", None, tool_calls=[{"id": "call-1", "function": {"name": "read_artifact", "arguments": "{}"}}])
    session.add_message("tool", "full old artifact", tool_call_id="call-1", metadata={"artifact_access": {
        "artifact_ref": "artifact://old", "mode": "read", "returned_range": [0, 17], "eof": True,
        "response_sha256": evidence_response_digest("full old artifact"),
    }})
    output = [OPAQUE, MESSAGE] if compressed else [MESSAGE]
    invoke(session, provider(Client([response(output)])))
    visible = session.metadata["resource_state"]["artifact_prompt_visibility"]
    assert bool(visible) is not compressed


def test_failed_native_request_keeps_existing_artifact_visibility():
    session = seeded()
    session.metadata["resource_state"] = {"artifact_prompt_visibility": {"artifact://previous": {"eof": True}}}
    before = deepcopy(session.metadata["resource_state"])
    with pytest.raises(RuntimeError, match="offline"):
        invoke(session, provider(Client([RuntimeError("offline")])))
    assert session.metadata["resource_state"] == before


@pytest.mark.parametrize("outcome", ["success", "count_overflow", "transport_failure"])
def test_real_reasoning_loop_uses_compacted_native_window_and_transactional_visibility(outcome):
    from runtime.execution.reasoning_loop import ReasoningLoop
    from runtime.context.artifact_access import evidence_response_digest
    from tests.fakes import make_agent_spec
    from tests.test_long_request_externalization import _Tools
    from tools.executor import ToolExecutor

    session = seeded()
    session.metadata["context_policy"] = "native"
    session.add_message("assistant", None, tool_calls=[{"id": "old-read", "type": "function",
        "function": {"name": "read_artifact", "arguments": "{}"}}])
    huge = "unneeded old observation " * 15_000
    session.add_message("tool", huge, tool_call_id="old-read", metadata={"artifact_access": {
        "artifact_ref": "artifact://old", "mode": "read", "returned_range": [0, len(huge)], "eof": True,
        "response_sha256": evidence_response_digest(huge),
    }})
    session.metadata["context_compaction_request"] = {"status": "pending", "message_count": len(session.messages)}
    next_result = RuntimeError("generation offline") if outcome == "transport_failure" else response()
    client = Client([response(), next_result], compactions=[compacted([USER, OPAQUE])])
    native_provider = provider(client)
    first = invoke(session, native_provider)
    session.add_message(**first.raw_message)
    client.count_calls.clear()
    client.create_calls.clear()
    if outcome == "count_overflow":
        client.count_result = 50_000
    session.metadata["resource_state"]["artifact_prompt_visibility"] = {"artifact://previous": {"eof": True}}
    prior_visibility = deepcopy(session.metadata["resource_state"])
    prior_window = deepcopy(session.metadata[NATIVE_CONTEXT_KEY])
    context = NS(messages=[{"role": "system", "content": "current task"},
                           {"role": "developer", "content": "preserve public API"}, *session.messages])
    loop = ReasoningLoop(tools=_Tools(), tool_executor=ToolExecutor([]), max_tokens=200)
    kwargs = dict(session=session, context=context, agent_spec=make_agent_spec("bot", "bot", "bot"),
                  resolve_provider=lambda *_: (native_provider, "fixture"), tools_for_turn=[])
    if outcome == "success":
        result = loop._reasoning_step(**kwargs)
        assert result.content == "Continue tracing"
        assert client.create_calls[0]["max_output_tokens"] == 200
        assert session.metadata["resource_state"]["artifact_prompt_visibility"] == {}
    else:
        error = PromptBudgetExceeded if outcome == "count_overflow" else RuntimeError
        with pytest.raises(error):
            loop._reasoning_step(**kwargs)
        assert session.metadata["resource_state"] == prior_visibility
        assert session.metadata[NATIVE_CONTEXT_KEY] == prior_window
        if outcome == "count_overflow":
            assert not client.create_calls
    assert len(client.count_calls) == 1
    assert client.count_calls[0]["input"] == [USER, OPAQUE, MESSAGE]
    assert "preserve public API" in client.count_calls[0]["instructions"]
    assert huge not in str(client.count_calls[0])
