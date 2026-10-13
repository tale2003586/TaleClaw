"""Provider cache reads are reported counters, never inferred from local prefixes."""
from types import SimpleNamespace as NS

import pytest

from models.provider import OpenAICompatibleProvider, _usage_from_response
from runtime.execution.budget import ExecutionBudget, execution_scope
from runtime.execution.reasoning_loop import _usage_payload
from runtime.trace.run_state import RunState
from runtime.trace.trace_store import TraceStore
from tests.test_provider_contract import Client, Stream, chat_response, chunk, invoke


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
@pytest.mark.parametrize("as_object", [False, True])
@pytest.mark.parametrize("cached", [0, 6])
def test_provider_preserves_reported_cache_reads(api, as_object, cached):
    details = {"cached_tokens": cached}
    usage = ({"prompt_tokens": 8, "completion_tokens": 2, "prompt_tokens_details": NS(**details) if as_object else details}
             if api == "chat_completions" else
             {"input_tokens": 8, "output_tokens": 2, "input_tokens_details": NS(**details) if as_object else details})
    usage["total_tokens"] = 10
    response = NS(usage=NS(**usage)) if as_object else {"usage": usage}
    parsed = _usage_from_response(response)
    assert parsed.cached_input_tokens == cached
    assert (parsed.input_tokens, parsed.output_tokens, parsed.total_tokens) == (8, 2, 10)


@pytest.mark.parametrize("details", [None, {}, {"cached_tokens": None}, {"cached_tokens": True},
    {"cached_tokens": "4"}, {"cached_tokens": 4.0}, {"cached_tokens": -1}, {"cached_tokens": 9}])
def test_missing_or_invalid_cache_reads_do_not_invent_zero_or_fail_usage(details):
    parsed = _usage_from_response({"usage": {"prompt_tokens": 8, "completion_tokens": 2,
        "total_tokens": 10, "prompt_tokens_details": details}})
    assert parsed.cached_input_tokens is None
    assert parsed.total_tokens == 10


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
@pytest.mark.parametrize("streaming", [False, True])
def test_cache_reads_reach_provider_result_without_discounting_token_budget(api, streaming):
    if api == "chat_completions":
        usage = NS(prompt_tokens=8, completion_tokens=2, total_tokens=10, prompt_tokens_details=NS(cached_tokens=6))
        source = Stream([chunk("ok"), chunk(reason="stop"), NS(choices=[], usage=usage)]) if streaming else chat_response(usage=usage)
    else:
        usage = NS(input_tokens=8, output_tokens=2, total_tokens=10, input_tokens_details=NS(cached_tokens=6))
        response = NS(status="completed", output=[], output_text="ok", usage=usage)
        source = Stream([NS(type="response.completed", response=response)]) if streaming else response
    budget = ExecutionBudget()
    with execution_scope(budget=budget):
        result = invoke(OpenAICompatibleProvider(Client(source), wire_api=api), stream=streaming)
    assert result.usage.cached_input_tokens == 6
    assert (budget.model_attempts, budget.known_tokens, budget.unknown_usage_attempts) == (1, 10, 0)
    assert _usage_payload(result.usage)["cached_input_tokens"] == 6


@pytest.mark.parametrize("as_object", [False, True])
def test_custom_provider_usage_keeps_explicit_zero_cache_read(as_object):
    usage = {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10, "cached_input_tokens": 0}
    assert _usage_payload(NS(**usage) if as_object else usage)["cached_input_tokens"] == 0


def _trace_metrics(tmp_path, values, *, retry_count=0, failed_attempts=0, root_budget=None):
    store = TraceStore(tmp_path, index_enabled=False)
    state = RunState(run_id="cache-fixture", session_id="fixture",
                     metadata={"execution_budget": root_budget} if root_budget else {})
    for value in values:
        usage = {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10}
        if value != "missing":
            usage["cached_input_tokens"] = value
        store.append_event(state, "model.call.completed", {
            "usage": usage, "provider_metadata": {"retry_count": retry_count},
        })
    if failed_attempts:
        store.append_event(state, "model.call.failed", {"route_attempts": [{}] * failed_attempts})
    return store._metrics_for(state)


@pytest.mark.parametrize("values, expected, unknown", [
    ([], None, 0), (["missing"], None, 1), ([None], None, 1), ([0], 0, 0),
    ([6, 2], 8, 0), ([6, "missing"], 6, 1), ([True], None, 1),
    ([-1], None, 1), ([9], None, 1),
])
def test_trace_cache_subtotal_has_explicit_completeness(tmp_path, values, expected, unknown):
    metrics = _trace_metrics(tmp_path, values)
    assert metrics["cached_input_tokens"] == expected
    assert metrics["unknown_cache_usage_calls"] == unknown
    assert metrics["cache_usage_scope"] == "trace_main_calls"


def test_trace_cache_subtotal_marks_hidden_retry_and_failed_attempts_unknown(tmp_path):
    metrics = _trace_metrics(tmp_path, [6], retry_count=1, failed_attempts=2)
    assert metrics["cached_input_tokens"] == 6
    assert metrics["unknown_cache_usage_calls"] == 3


def test_root_budget_does_not_relabel_main_loop_cache_subtotal(tmp_path):
    metrics = _trace_metrics(tmp_path, [6], root_budget={
        "model_attempts": 3, "known_tokens": 30, "unknown_usage_attempts": 0,
    })
    assert metrics["total_tokens"] == 30
    assert metrics["usage_scope"] == "root_all_purposes"
    assert metrics["cached_input_tokens"] == 6
    assert metrics["cache_usage_scope"] == "trace_main_calls"
    assert metrics["unknown_cache_usage_calls"] == 0


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
def test_missing_input_counter_still_bounds_cache_reads_by_known_total(api):
    details = "prompt_tokens_details" if api == "chat_completions" else "input_tokens_details"
    result = _usage_from_response({"usage": {"total_tokens": 10, details: {"cached_tokens": 11}}})
    assert result.cached_input_tokens is None
    assert result.total_tokens == 10


def test_trace_rejects_cache_counter_larger_than_known_total(tmp_path):
    store = TraceStore(tmp_path, index_enabled=False)
    state = RunState(run_id="invalid-cache", session_id="fixture")
    store.append_event(state, "model.call.completed", {
        "usage": {"total_tokens": 10, "cached_input_tokens": 11},
    })
    metrics = store._metrics_for(state)
    assert metrics["cached_input_tokens"] is None
    assert metrics["unknown_cache_usage_calls"] == 1
