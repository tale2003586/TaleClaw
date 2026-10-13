"""Provider safety contracts; all clients and responses are local fakes."""
from time import monotonic
from types import SimpleNamespace as NS

import pytest

from models.provider import LLMUsage, ModelProtocolError, OpenAICompatibleProvider
from models.model_pool import ModelPool, ModelProfile, ModelRouteError
from runtime.context.dynamic_budget import PromptBudgetExceeded
from runtime.execution.budget import (
    ExecutionBudget, ExecutionBudgetExceeded, ExecutionCancelled, execution_scope,
)


class Endpoint:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result() if callable(self.result) else self.result


class Client:
    def __init__(self, result):
        self.endpoint = Endpoint(result)
        self.chat = NS(completions=self.endpoint)
        self.responses = self.endpoint
        self.options = []

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        self.closed = True


def chat_response(arguments=None, reason="stop", usage=None):
    tools = [] if arguments is None else [NS(id="call-1", function=NS(name="inspect", arguments=arguments))]
    message = NS(content="ok", tool_calls=tools, model_dump=lambda **_: {"role": "assistant", "content": "ok"})
    return NS(choices=[NS(message=message, finish_reason=reason)], usage=usage)


def chunk(content=None, reason=None, arguments=None, usage=None):
    calls = [] if arguments is None else [NS(index=0, id="call-1", function=NS(name="inspect", arguments=arguments))]
    return NS(choices=[NS(delta=NS(content=content, tool_calls=calls), finish_reason=reason)], usage=usage)


def invoke(provider, stream=False, **overrides):
    kwargs = dict(messages=[{"role": "user", "content": "inspect"}], tools=[], model="fixture", max_tokens=20)
    kwargs.update(overrides)
    if stream:
        kwargs.setdefault("on_text", lambda _: None)
        return provider.stream_chat(**kwargs)
    return provider.chat(**kwargs)


@pytest.mark.parametrize("arguments", ["{bad", "[]", "null", "123", '"text"', '{"number": NaN}', ""])
@pytest.mark.parametrize("api", ["chat_completions", "responses"])
def test_malformed_or_nonobject_tool_arguments_are_typed_failure(arguments, api):
    response = chat_response(arguments, reason="tool_calls") if api == "chat_completions" else NS(
        status="completed", output=[NS(type="function_call", call_id="c", name="inspect", arguments=arguments)],
    )
    with pytest.raises(ModelProtocolError):
        invoke(OpenAICompatibleProvider(Client(response), wire_api=api))


@pytest.mark.parametrize("reason", ["length", "content_filter", None])
def test_incomplete_chat_cannot_return_text_or_tool_calls(reason):
    with pytest.raises(ModelProtocolError):
        invoke(OpenAICompatibleProvider(Client(chat_response("{}", reason=reason))))


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
def test_stream_without_completion_is_error_and_closes(api):
    chunks = [chunk("partial", arguments="{}")] if api == "chat_completions" else [
        NS(type="response.output_text.delta", delta="partial"),
        NS(type="response.function_call_arguments.done", output_index=0, name="inspect", arguments="{}"),
    ]
    stream = Stream(chunks)
    with pytest.raises(ModelProtocolError):
        invoke(OpenAICompatibleProvider(Client(stream), wire_api=api), stream=True)
    assert stream.closed


def test_response_incomplete_preserves_known_usage_but_is_never_success():
    usage = NS(input_tokens=8, output_tokens=2, total_tokens=10)
    stream = Stream([NS(type="response.incomplete", response=NS(status="incomplete", usage=usage))])
    budget = ExecutionBudget()
    with execution_scope(budget=budget), pytest.raises(ModelProtocolError):
        invoke(OpenAICompatibleProvider(Client(stream), wire_api="responses"), stream=True)
    assert (budget.model_attempts, budget.known_tokens, budget.unknown_usage_attempts) == (1, 10, 0)
    assert stream.closed


def test_chat_stream_final_usage_counted_once_after_terminal_chunk():
    usage = NS(prompt_tokens=8, completion_tokens=2, total_tokens=10)
    stream = Stream([chunk("hello"), chunk(reason="stop"), NS(choices=[], usage=usage)])
    budget = ExecutionBudget()
    with execution_scope(budget=budget):
        result = invoke(OpenAICompatibleProvider(Client(stream)), stream=True)
    assert result.content == "hello"
    assert result.usage.total_tokens == 10
    assert (budget.model_attempts, budget.known_tokens, budget.unknown_usage_attempts) == (1, 10, 0)


def test_transport_failure_records_one_attempt_and_unknown_usage():
    budget = ExecutionBudget()
    with execution_scope(budget=budget), pytest.raises(RuntimeError, match="offline"):
        invoke(OpenAICompatibleProvider(Client(RuntimeError("offline"))))
    assert (budget.model_attempts, budget.unknown_usage_attempts) == (1, 1)


@pytest.mark.parametrize("root", [
    lambda: ExecutionBudget(cancel_requested=lambda: True),
    lambda: ExecutionBudget(deadline=monotonic() - 1),
    lambda: ExecutionBudget(max_model_attempts=0),
])
def test_cancel_deadline_attempt_limit_block_before_network(root):
    client = Client(chat_response())
    with execution_scope(budget=root()), pytest.raises((ExecutionBudgetExceeded, ExecutionCancelled)):
        invoke(OpenAICompatibleProvider(client))
    assert not client.endpoint.calls


def test_cancellation_during_stream_closes_without_returning_action():
    cancelled = False
    def on_text(_):
        nonlocal cancelled
        cancelled = True
    stream = Stream([chunk("partial"), chunk(reason="tool_calls", arguments="{}")])
    budget = ExecutionBudget(cancel_requested=lambda: cancelled)
    with execution_scope(budget=budget), pytest.raises(ExecutionCancelled):
        invoke(OpenAICompatibleProvider(Client(stream)), stream=True, on_text=on_text)
    assert stream.closed
    assert budget.model_attempts == 1
    assert budget.unknown_usage_attempts == 1


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
@pytest.mark.parametrize("content,tools,max_tokens", [
    ("x" * 10000, [], 20),
    ("tiny", [{"type": "function", "function": {"name": "x", "description": "x" * 10000}}], 20),
    ("tiny", [], 1010),
])
def test_final_request_guard_covers_input_schemas_and_full_output_reserve(api, content, tools, max_tokens):
    client = Client(chat_response())
    budget = ExecutionBudget()
    provider = OpenAICompatibleProvider(client, wire_api=api, context_window_tokens=1000)
    with execution_scope(budget=budget), pytest.raises(PromptBudgetExceeded):
        invoke(provider, messages=[{"role": "user", "content": content}], tools=tools, max_tokens=max_tokens)
    assert not client.endpoint.calls
    assert budget.model_attempts == 0
    assert budget.unknown_usage_attempts == 0


def profiles_pool():
    return ModelPool(profiles={name: ModelProfile(
        name=name, provider="fixture", api_key="unused", base_url="https://unused.invalid", model="fixture",
    ) for name in ("a", "b")}, routes={"chat": ("a", "b")}, default_profile="a")


@pytest.mark.parametrize("error", [ModelProtocolError("bad"), ExecutionCancelled("stop"), ExecutionBudgetExceeded("limit")])
def test_nonretryable_errors_do_not_fallback_or_pollute_health(error):
    pool = profiles_pool()
    first = Client(error)
    second = Client(chat_response())
    pool._providers = {"a": OpenAICompatibleProvider(first), "b": OpenAICompatibleProvider(second)}
    with execution_scope(), pytest.raises(type(error)):
        invoke(pool.routed_provider("chat"))
    assert not second.endpoint.calls
    assert pool.profile_health("a")["consecutive_failures"] == 0


def test_all_open_route_fails_without_creating_a_client():
    pool = profiles_pool()
    for name in ("a", "b"):
        pool.mark_profile_failure(name, RuntimeError("down"), force_disable=True)
    with pytest.raises(ModelRouteError):
        invoke(pool.routed_provider("chat"))
    assert pool._clients == {}


def test_fallback_attempts_share_root_budget_and_usage_accounting():
    pool = profiles_pool()
    pool._providers = {
        "a": OpenAICompatibleProvider(Client(RuntimeError("offline"))),
        "b": OpenAICompatibleProvider(Client(chat_response(usage=NS(total_tokens=7)))),
    }
    budget = ExecutionBudget()
    with execution_scope(budget=budget):
        result = invoke(pool.routed_provider("chat"))
    assert result.provider_metadata["selected_profile"] == "b"
    assert (budget.model_attempts, budget.known_tokens, budget.unknown_usage_attempts) == (2, 7, 1)


def test_sdk_retries_disabled_and_timeout_limited_by_remaining_deadline():
    client = Client(chat_response())
    root = ExecutionBudget(deadline=monotonic() + 3)
    with execution_scope(budget=root):
        invoke(OpenAICompatibleProvider(client))
    assert client.options[0]["max_retries"] == 0
    assert 0 < client.options[0]["timeout"] <= 3
    seen = []
    pool = profiles_pool()
    pool.client_factory = lambda **kwargs: seen.append(kwargs) or client
    pool.client_for_profile("a")
    assert seen[0]["max_retries"] == 0
    assert seen[0]["timeout"] == 60


@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_chat_stream_incomplete_terminal_cannot_release_tool_arguments(reason):
    stream = Stream([chunk(arguments="{}"), chunk(reason=reason)])
    with pytest.raises(ModelProtocolError):
        invoke(OpenAICompatibleProvider(Client(stream)), stream=True)
    assert stream.closed


@pytest.mark.parametrize("status", ["incomplete", "failed", "in_progress", None])
def test_responses_nonstream_requires_completed_status(status):
    with pytest.raises(ModelProtocolError):
        invoke(OpenAICompatibleProvider(Client(NS(status=status, output=[])), wire_api="responses"))


def test_duplicate_tool_ids_are_not_released_to_executor():
    response = chat_response("{}", reason="tool_calls")
    response.choices[0].message.tool_calls *= 2
    with pytest.raises(ModelProtocolError, match="unique"):
        invoke(OpenAICompatibleProvider(Client(response)))


@pytest.mark.parametrize("late", [
    chunk("late"), chunk(arguments="{}"),
    NS(choices=[NS(delta=NS(reasoning_content="late thought"), finish_reason=None)]),
    chunk(reason="stop"),
])
def test_chat_terminal_rejects_every_later_choice_and_closes(late):
    seen = []
    stream = Stream([chunk("finished", reason="stop"), late])
    with pytest.raises(ModelProtocolError, match="after its terminal"):
        invoke(OpenAICompatibleProvider(Client(stream)), stream=True, on_text=seen.append)
    assert seen == ["finished"]
    assert stream.closed


def test_chat_terminal_chunk_itself_can_contain_final_tool_delta():
    stream = Stream([chunk(reason="tool_calls", arguments="{}"), NS(choices=[], usage=NS(total_tokens=2))])
    result = invoke(OpenAICompatibleProvider(Client(stream)), stream=True)
    assert [(call.id, call.name, call.arguments) for call in result.tool_calls] == [("call-1", "inspect", {})]
    assert stream.closed and result.usage.total_tokens == 2


@pytest.mark.parametrize("final", [
    [], [NS(type="function_call", call_id="other", name="write_file", arguments='{"path":"a"}')],
    [NS(type="function_call", call_id="c", name="remove_file", arguments='{"path":"a"}')],
    [NS(type="function_call", call_id="c", name="write_file", arguments='{"path":"b"}')],
])
def test_responses_final_object_must_confirm_intermediate_tools(final):
    stream = Stream([
        NS(type="response.output_item.added", output_index=0,
           item=NS(type="function_call", call_id="c", name="write_file", arguments='{"path":"a"}')),
        NS(type="response.completed", response=NS(status="completed", output=final)),
    ])
    with pytest.raises(ModelProtocolError):
        invoke(OpenAICompatibleProvider(Client(stream), wire_api="responses"), stream=True)
    assert stream.closed


def test_responses_completed_rejects_later_delta():
    stream = Stream([
        NS(type="response.completed", response=NS(status="completed", output=[])),
        NS(type="response.output_text.delta", delta="late"),
    ])
    seen = []
    with pytest.raises(ModelProtocolError, match="after response.completed"):
        invoke(OpenAICompatibleProvider(Client(stream), wire_api="responses"), stream=True, on_text=seen.append)
    assert not seen and stream.closed


@pytest.mark.parametrize("response", [
    NS(choices=[NS(message=NS(content="broken"), finish_reason="stop")]),
    NS(choices=[NS(finish_reason="stop")]),
    NS(choices="not an array"),
    NS(choices=[NS(message=NS(content="ok", tool_calls=[NS(id="c")]), finish_reason="tool_calls")]),
])
def test_malformed_response_structure_is_nonretryable_and_does_not_pollute_health(response):
    first, second = Client(response), Client(chat_response())
    pool = profiles_pool()
    pool._providers = {"a": OpenAICompatibleProvider(first), "b": OpenAICompatibleProvider(second)}
    with pytest.raises(ModelProtocolError):
        invoke(pool.routed_provider("chat"))
    assert not second.endpoint.calls
    assert pool.profile_health("a")["consecutive_failures"] == 0


@pytest.mark.parametrize("usage", [
    NS(total_tokens=0, prompt_tokens=800, completion_tokens=200),
    NS(total_tokens=1001, input_tokens=800, output_tokens=200),
    NS(total_tokens=1, prompt_tokens=2),
])
def test_inconsistent_usage_stops_and_records_unknown_once(usage):
    budget = ExecutionBudget()
    with execution_scope(budget=budget), pytest.raises(ModelProtocolError, match="usage counters"):
        invoke(OpenAICompatibleProvider(Client(chat_response(usage=usage))))
    assert (budget.model_attempts, budget.known_tokens, budget.unknown_usage_attempts) == (1, 0, 1)
    assert budget.snapshot()["usage_complete"] is False


def test_inconsistent_final_usage_does_not_reuse_earlier_partial_counter():
    stream = Stream([
        chunk("text", reason="stop", usage=NS(total_tokens=2)),
        NS(choices=[], usage=NS(total_tokens=0, prompt_tokens=3, completion_tokens=1)),
    ])
    budget = ExecutionBudget()
    with execution_scope(budget=budget), pytest.raises(ModelProtocolError):
        invoke(OpenAICompatibleProvider(Client(stream)), stream=True)
    assert stream.closed
    assert (budget.model_attempts, budget.known_tokens, budget.unknown_usage_attempts) == (1, 0, 1)


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
def test_callback_cancellation_stops_before_reading_another_stream_chunk(api):
    cancelled = False
    reads = []

    def chunks():
        reads.append("first")
        yield chunk("partial") if api == "chat_completions" else NS(
            type="response.output_text.delta", delta="partial",
        )
        reads.append("unwanted second read")
        raise AssertionError("cancelled stream advanced to another blocking read")

    def on_text(_):
        nonlocal cancelled
        cancelled = True

    stream = Stream(chunks())
    budget = ExecutionBudget(cancel_requested=lambda: cancelled)
    with execution_scope(budget=budget), pytest.raises(ExecutionCancelled):
        invoke(OpenAICompatibleProvider(Client(stream), wire_api=api), stream=True, on_text=on_text)
    assert reads == ["first"]
    assert stream.closed
    assert (budget.model_attempts, budget.unknown_usage_attempts) == (1, 1)


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
def test_cancellation_while_opening_stream_closes_without_reading(api):
    reads = []
    cancelled = False

    def chunks():
        reads.append("unwanted first read")
        raise AssertionError("cancelled stream started a blocking read")
        yield

    stream = Stream(chunks())

    def open_stream():
        nonlocal cancelled
        cancelled = True
        return stream

    budget = ExecutionBudget(cancel_requested=lambda: cancelled)
    with execution_scope(budget=budget), pytest.raises(ExecutionCancelled):
        invoke(OpenAICompatibleProvider(Client(open_stream), wire_api=api), stream=True)
    assert reads == []
    assert stream.closed
    assert (budget.model_attempts, budget.unknown_usage_attempts) == (1, 1)
