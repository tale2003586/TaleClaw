"""Automatic security retrieval is a run-local observation, not one-step input."""
from types import SimpleNamespace as NS

import pytest

from runtime.context.providers import RetrievalContextProvider
from runtime.execution.state import RunExecutionState
from runtime.extensions import RuntimeExtensions
from runtime.runtime import RunContext, Runtime, _last_user_message_index


class Retrieval:
    def __init__(self):
        self.security_calls = []
        self.history_calls = []
        self.empty = False

    def retrieve_history(self, **kwargs):
        self.history_calls.append(kwargs)
        return f"history-{len(self.history_calls)}", []

    def retrieve_security(self, **kwargs):
        self.security_calls.append(kwargs)
        if self.empty:
            return "", None, []
        revision = len(self.security_calls)
        return f"security-{revision}", NS(revision=revision), [f"hit-{revision}"]


def provide(service, security_context, *, session_id="one", request="current request", enabled=True):
    builder = NS(retrieval_service=service, budgeter=NS(apply=lambda name, text: text))
    return RetrievalContextProvider().provide(
        builder, session=NS(id=session_id), current_request=request,
        active_turn_messages=[], include_security_knowledge=enabled,
        security_context=security_context,
    )


def test_security_snapshot_stays_in_later_steps_while_history_refreshes():
    service, cache = Retrieval(), {}
    first = provide(service, cache)
    second = provide(service, cache)
    assert (first.raw_security, second.raw_security) == ("security-1", "security-1")
    assert first.security_decision == second.security_decision
    assert first.security_hits == second.security_hits == ["hit-1"]
    assert (first.raw_history, second.raw_history) == ("history-1", "history-2")
    assert len(service.security_calls) == 1


@pytest.mark.parametrize("change", ["session", "request", "service"])
def test_security_snapshot_refreshes_on_identity_change(change):
    service, cache = Retrieval(), {}
    provide(service, cache)
    if change == "session":
        provide(service, cache, session_id="two")
    elif change == "request":
        provide(service, cache, request="revised request")
    else:
        replacement = Retrieval()
        provide(replacement, cache)
        assert len(replacement.security_calls) == 1
        return
    assert len(service.security_calls) == 2


def test_run_state_reset_discards_snapshot_and_instances_do_not_share_it():
    service = Retrieval()
    state = RunExecutionState()
    provide(service, state.security_context)
    assert RunExecutionState().security_context == {}
    state.reset(web_search_limit=3)
    assert state.security_context == {}
    provide(service, state.security_context)
    assert len(service.security_calls) == 2


def test_explicit_exclusion_cannot_inject_cached_security():
    service, cache = Retrieval(), {}
    provide(service, cache)
    excluded = provide(service, cache, enabled=False)
    assert (excluded.raw_security, excluded.security_decision, excluded.security_hits) == ("", None, [])
    assert len(service.security_calls) == 1
    assert provide(service, cache).raw_security == "security-1"
    assert len(service.security_calls) == 1


def test_empty_observation_is_cached_but_state_less_calls_stay_fresh():
    service, cache = Retrieval(), {}
    service.empty = True
    assert provide(service, cache).raw_security == ""
    assert provide(service, cache).raw_security == ""
    assert len(service.security_calls) == 1
    provide(service, None)
    provide(service, None)
    assert len(service.security_calls) == 3


def test_explicit_fresh_retrieval_does_not_replace_automatic_snapshot():
    service, cache = Retrieval(), {}
    assert provide(service, cache).raw_security == "security-1"
    assert service.retrieve_security(current_request="current request")[0] == "security-2"
    assert provide(service, cache).raw_security == "security-1"
    assert len(service.security_calls) == 2


def test_runtime_forwards_same_snapshot_and_does_not_disable_later_steps():
    calls = []
    rendered = NS(report=NS(to_dict=lambda: {"sections": {"security_knowledge": {"rendered_chars": 10}}}))
    builder = NS(build=lambda **kwargs: calls.append(kwargs) or rendered)
    runtime = object.__new__(Runtime)
    runtime.agent_runner = NS(context_builder=builder)
    context = RunContext(session=NS(id="one"))
    for _ in range(2):
        runtime._before_reasoning(context.session, NS(), run_context=context)
    assert all(call["include_security_knowledge"] is True for call in calls)
    assert all(call["security_context"] is context.state.security_context for call in calls)
    runtime._before_reasoning(context.session, NS(), run_context=NS(state=None, extensions=RuntimeExtensions()))
    assert calls[-1]["security_context"] is None


@pytest.mark.parametrize("provenance", [
    {"source": "runtime-generated"}, {"origin": "tool"},
    {"kind": "reflection_instruction"}, {"kind": "budget_reminder"},
])
def test_current_request_index_ignores_runtime_user_transport(provenance):
    messages = [
        {"role": "user", "content": "real current request"},
        {"role": "assistant", "content": "working"},
        {"role": "user", "content": "host wrapper", "metadata": provenance},
    ]
    assert _last_user_message_index(messages) == 0
    assert _last_user_message_index(messages[1:]) is None


def test_legacy_real_users_and_explicit_human_origin_remain_valid():
    assert _last_user_message_index([{"role": "user", "content": "legacy"}]) == 0
    assert _last_user_message_index([{"role": "user", "content": "human", "metadata": {"origin": "human"}}]) == 0


def test_real_runtime_builder_reinjects_snapshot_and_refreshes_memory():
    from agents.definitions import BOT_AGENT_SPEC
    from runtime.context import ContextBuilder
    from runtime.context.providers import DEFAULT_CONTEXT_PROVIDERS
    from runtime.sessions import Session

    service, memory_calls = Retrieval(), []
    memory = NS(build_memory_block=lambda session, **kwargs: memory_calls.append(kwargs) or "memory")
    builder = ContextBuilder(
        context_providers=DEFAULT_CONTEXT_PROVIDERS,
        retrieval_service=service, memory_service=memory,
    )
    runtime = object.__new__(Runtime)
    runtime.agent_runner = NS(context_builder=builder)
    session = Session(id="security-lifetime")
    session.add_message("user", "current request")
    context = RunContext(session=session)
    first = runtime._before_reasoning(session, BOT_AGENT_SPEC, run_context=context)
    session.add_message("assistant", "continue reasoning")
    second = runtime._before_reasoning(session, BOT_AGENT_SPEC, run_context=context)
    for bundle in (first, second):
        assert any("security-1" in str(item.get("content")) for item in bundle.messages)
        assert bundle.report.to_dict()["sections"]["security_knowledge"]["rendered_chars"] > 0
    assert len(service.security_calls) == 1
    assert len(service.history_calls) == len(memory_calls) == 2
