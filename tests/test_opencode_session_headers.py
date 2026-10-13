"""OpenCode Go header compatibility; all requests use in-process fakes."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Barrier
from types import SimpleNamespace as NS

import pytest

from models.provider import OpenAICompatibleProvider
from runtime.execution.budget import (
    ExecutionBudget, current_budget, execution_scope, model_request_session_id,
)
from tests.test_provider_contract import Client, Stream, chat_response, chunk, invoke
from tests.test_native_compaction_regressions import Client as NativeClient, response, compacted, USER


def make_provider(url="https://opencode.ai/zen/go/v1"):
    client = Client(chat_response())
    client.base_url = url
    client.default_headers = {"User-Agent": "configured-agent", "x-test": "retained"}
    return OpenAICompatibleProvider(client), client


def header(client, index=-1):
    return client.endpoint.calls[index]["extra_headers"]["x-opencode-session"]


def test_same_conversation_resumes_across_budgets_and_new_tasks_isolate():
    provider, client = make_provider()
    for session in ("private-user:task-a", "private-user:task-a", "private-user:task-b"):
        with execution_scope(session_id=session):
            invoke(provider)
    ids = [call["extra_headers"]["x-opencode-session"] for call in client.endpoint.calls]
    assert ids[0] == ids[1] != ids[2]
    assert all(value.startswith("taleclaw-") and len(value) == 73 for value in ids)
    assert all("private-user" not in value and "task-a" not in value for value in ids)
    assert all(call["extra_headers"]["User-Agent"] == "TaleClaw" for call in client.endpoint.calls)
    assert client.default_headers == {"User-Agent": "configured-agent", "x-test": "retained"}


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "https://opencode.ai.example/v1",
                                  "https://sub.opencode.ai/v1", "https://example.com/opencode.ai"])
def test_opencode_headers_never_added_to_other_hosts(url):
    provider, client = make_provider(url)
    with execution_scope(session_id="private-session"):
        invoke(provider)
    assert "extra_headers" not in client.endpoint.calls[0]


def test_scoped_fallback_identity_is_stable_and_unscoped_calls_are_distinct():
    provider, client = make_provider()
    with execution_scope():
        invoke(provider)
        invoke(provider)
    assert header(client, 0) == header(client, 1)
    invoke(provider)
    invoke(provider)
    assert len({header(client, 0), header(client, 2), header(client, 3)}) == 3


def test_nested_identity_restores_on_error_without_closing_shared_budget():
    budget = ExecutionBudget()
    with execution_scope(budget=budget, session_id="parent"):
        parent = model_request_session_id()
        with pytest.raises(RuntimeError):
            with execution_scope(session_id="child") as nested:
                assert nested is budget
                assert model_request_session_id() != parent
                raise RuntimeError("fixture")
        assert model_request_session_id() == parent
        assert current_budget() is budget and not budget.closed
        with execution_scope():
            assert model_request_session_id() == parent
    assert current_budget() is None and budget.closed
    with execution_scope(session_id="child"):
        assert model_request_session_id() != parent


def test_shared_provider_parallel_sessions_do_not_cross_or_mutate_headers():
    provider, client = make_provider()
    barrier = Barrier(2)

    def run(session):
        with execution_scope(session_id=session):
            expected = model_request_session_id()
            barrier.wait(timeout=5)
            invoke(provider, messages=[{"role": "user", "content": session}])
            return session, expected

    with ThreadPoolExecutor(2) as pool:
        expected = dict(pool.map(run, ["conversation-a", "conversation-b"]))
    for call in client.endpoint.calls:
        assert call["extra_headers"]["x-opencode-session"] == expected[call["messages"][0]["content"]]
    assert len(set(expected.values())) == 2
    assert "x-opencode-session" not in client.default_headers


def test_existing_thread_context_inherits_session_identity():
    with execution_scope(session_id="conversation-thread"):
        expected = model_request_session_id()
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(copy_context().run, model_request_session_id).result() == expected


def test_stream_and_auxiliary_provider_share_same_conversation():
    stream = Stream([chunk("hello"), chunk(reason="stop")])
    client = Client(stream)
    client.base_url = "https://opencode.ai/zen/go/v1"
    provider = OpenAICompatibleProvider(client)
    auxiliary, auxiliary_client = make_provider()
    with execution_scope(session_id="task:stream"):
        result = invoke(provider, stream=True)
        invoke(auxiliary)
    assert result.content == "hello" and stream.closed
    assert header(client) == header(auxiliary_client)


@pytest.mark.parametrize("streaming", [False, True])
def test_native_create_count_and_compact_inherit_same_identity_without_scope(streaming):
    result = Stream([NS(type="response.completed", response=response())]) if streaming else response()
    client = NativeClient([result], compactions=[compacted()])
    client.base_url = "https://opencode.ai/zen/go/v1"
    provider = OpenAICompatibleProvider(client, wire_api="responses", native_compaction_supported=True)
    provider.native_chat(input_items=[USER], tools=[], model="fixture", max_tokens=20,
                         on_text=(lambda _: None) if streaming else None)
    create_id = client.create_calls[0]["extra_headers"]["x-opencode-session"]
    assert client.count_calls[0]["extra_headers"]["x-opencode-session"] == create_id
    provider.compact_context(input_items=[USER], model="fixture")
    compact_id = client.compact_calls[0]["extra_headers"]["x-opencode-session"]
    assert client.count_calls[1]["extra_headers"]["x-opencode-session"] == compact_id
    assert create_id != compact_id
    assert all(call["extra_headers"]["User-Agent"] == "TaleClaw"
               for call in [*client.create_calls, *client.count_calls, *client.compact_calls])


def test_native_identity_does_not_depend_on_request_session():
    provider, _ = make_provider()
    before = provider.native_context_identity()
    with execution_scope(session_id="task-1"):
        invoke(provider)
    with execution_scope(session_id="task-2"):
        invoke(provider)
    assert provider.native_context_identity() == before


def test_transport_keeps_other_explicit_headers_without_mutating_request():
    provider, client = make_provider()
    original = {"extra_headers": {"user-agent": "do-not-impersonate", "X-OpenCode-Session": "wrong", "x-custom": "keep"}}
    with execution_scope(session_id="right"):
        provider._transport("chat", original)
    actual = client.endpoint.calls[0]["extra_headers"]
    assert actual["User-Agent"] == "TaleClaw"
    assert actual["x-custom"] == "keep"
    assert actual["x-opencode-session"] != "wrong"
    assert "user-agent" not in actual and "X-OpenCode-Session" not in actual
    assert original["extra_headers"]["X-OpenCode-Session"] == "wrong"


def test_runtime_binds_actual_session_over_parent_scope():
    from runtime.runtime import RunContext
    from runtime.sessions.session import Session
    from tests.fakes import make_agent_spec
    from tests.test_run_trace import _runtime, ScriptedProvider, _final_response

    observed = []

    class RecordingProvider(ScriptedProvider):
        def chat(self, **kwargs):
            observed.append(model_request_session_id())
            return super().chat(**kwargs)

    provider = RecordingProvider([_final_response("done"), _final_response("done")])
    runtime = _runtime(provider)
    for session_id in ("task:1", "task:2"):
        session = Session(session_id)
        session.add_message("user", "hello")
        with execution_scope(session_id="parent"):
            parent_identity = model_request_session_id()
            runtime.run(make_agent_spec("chat", "", "bot"), "hello", RunContext(session=session))
            assert model_request_session_id() == parent_identity
    assert len(observed) == 2 and observed[0] != observed[1]
    assert parent_identity not in observed
