"""Offline request-layout contracts; common prefixes are not provider KV hits."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from applications.coding.context_state import build_coding_context_view
from applications.coding.task_state import Objective, TaskState, save_task_state
from models.provider import OpenAICompatibleProvider
from runtime.context.assets import PromptAssetsService
from runtime.context.budget import ContextBudgeter
from runtime.context.builder import ContextBuilder
from runtime.context.dynamic_budget import reduce_prompt_to_hard_limit, select_complete_groups
from runtime.context.providers import (
    CodingContextProvider, EmptyRetrievalContextProvider, HistoryContextProvider,
    MemoryContext, NoCodingContextProvider, PromptContextProvider,
)
from runtime.extensions import ContextContribution
from runtime.sessions.session import Session
from tools.schema import function_tool
from tools.spec import ToolExposure, ToolSpec
from tools.tool_registry import ToolRegistry
from tests.test_provider_contract import Client, chat_response, invoke


class ChangingMemory:
    name = "memory"
    text = "retrieved-old-fact"

    def provide(self, builder, **kwargs):
        return MemoryContext(self.text, builder.budgeter.apply("memory", self.text))


def _tool_pair(session, suffix):
    session.add_message("assistant", None, tool_calls=[{
        "id": "call-" + suffix, "type": "function", "function": {
            "name": "inspect", "arguments": json.dumps({"path": suffix}),
        },
    }])
    session.add_message("tool", "observation-" + suffix, tool_call_id="call-" + suffix)


def _fixture(tmp_path, mode):
    tmp_path.mkdir(parents=True, exist_ok=True)
    budgeter = ContextBudgeter(enabled=False)
    memory = ChangingMemory()
    builder = ContextBuilder(
        budgeter=budgeter,
        prompt_assets_service=PromptAssetsService(budgeter=budgeter, instruction_root=tmp_path),
        context_providers=(PromptContextProvider(), HistoryContextProvider(), memory,
            EmptyRetrievalContextProvider(), CodingContextProvider() if mode == "coding" else NoCodingContextProvider()),
        coding_context_view_builder=build_coding_context_view if mode == "coding" else None,
        pressure_observation_enabled=False, injection_trace_enabled=False,
    )
    spec = NS(name="layout-fixture", instructions="Stable task rules.", tool_mode=mode)
    session = Session(id="offline:layout", active_agent=mode)
    session.add_message("user", "Inspect parser; preserve its public API.")
    _tool_pair(session, "first")
    state = TaskState(objective=Objective("Inspect parser; preserve its public API."), current_focus="old-focus")
    save_task_state(session, state)
    return builder, spec, session, memory, state


def _request(api, messages, tools=()):
    result = chat_response() if api == "chat_completions" else NS(status="completed", output=[], output_text="ok")
    client = Client(result)
    invoke(OpenAICompatibleProvider(client, wire_api=api), messages=messages, tools=list(tools))
    return copy.deepcopy(client.endpoint.calls[-1])


def _items(request):
    return request.get("messages", request.get("input", []))


def _prefix_metrics(first, second):
    # Canonical JSON of the actual SDK input projection, not tokenizer/server serialization.
    serialized = [json.dumps(_items(item), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                  for item in (first, second)]
    byte_count = 0
    for left, right in zip(*serialized):
        if left != right:
            break
        byte_count += 1
    item_count = 0
    for left, right in zip(_items(first), _items(second)):
        if left != right:
            break
        item_count += 1
    return {"canonical_input_json_common_prefix_bytes": byte_count,
            "complete_common_input_items": item_count,
            "first_input_items": len(_items(first)), "second_input_items": len(_items(second)),
            "metric_scope": "offline structure only; not tokens or provider cache hits"}


def layout_probe(tmp_path, mode, api):
    builder, spec, session, memory, state = _fixture(tmp_path, mode)
    first = builder.build(session=session, agent_spec=spec, active_turn_start_index=0)
    _tool_pair(session, "second")
    state.current_focus = "fresh-focus"
    state.version += 1
    save_task_state(session, state)
    memory.text = "retrieved-fresh-fact"
    second = builder.build(session=session, agent_spec=spec, active_turn_start_index=0)
    tools = [function_tool("inspect", "Inspect a path", {"path": {"type": "string"}})]
    requests = [_request(api, bundle.messages, tools) for bundle in (first, second)]
    return first, second, requests, session


@pytest.mark.parametrize("mode", ["teammate", "coding"])
@pytest.mark.parametrize("api", ["chat_completions", "responses"])
def test_live_updates_preserve_old_wire_trajectory_prefix(tmp_path, mode, api):
    first, second, requests, session = layout_probe(tmp_path, mode, api)
    assert _prefix_metrics(*requests)["complete_common_input_items"] >= 4
    # Real user, function call and result remain complete before live state/evidence.
    for bundle in (first, second):
        state_index = next(i for i, message in enumerate(bundle.messages)
                           if message.get("metadata", {}).get("kind") == "task_state_context")
        last_tool = max(i for i, message in enumerate(bundle.messages) if message["role"] == "tool")
        assert state_index > last_tool
    old, fresh = [json.dumps(_items(request)) for request in requests]
    assert "old-focus" in old and "fresh-focus" not in old
    assert "fresh-focus" in fresh and "old-focus" not in fresh
    assert "retrieved-fresh-fact" in fresh and "retrieved-old-fact" not in fresh
    assert len(session.messages) == 5
    assert all("metadata" not in item or item["metadata"].get("source") != "runtime-generated"
               for item in session.messages)
    if api == "responses":
        calls = [item["call_id"] for item in _items(requests[1]) if item.get("type") == "function_call"]
        results = [item["call_id"] for item in _items(requests[1]) if item.get("type") == "function_call_output"]
    else:
        calls = [call["id"] for item in _items(requests[1]) for call in item.get("tool_calls", [])]
        results = [item["tool_call_id"] for item in _items(requests[1]) if item.get("role") == "tool"]
    assert calls == results == ["call-first", "call-second"]


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
def test_evidence_boundary_survives_wire_metadata_removal(tmp_path, api):
    _, second, requests, _ = layout_probe(tmp_path, "coding", api)
    evidence = next(item for item in second.messages if item.get("metadata", {}).get("kind") == "retrieved_evidence_context")
    content = evidence["content"].lower()
    assert "runtime-generated" in content
    assert "untrusted" in content
    assert "do not change the task" in content
    assert "actual user request" in content
    assert any(item.get("content") == evidence["content"] for item in _items(requests[1]))


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
def test_permissions_change_wire_schemas_even_when_local_prompt_cache_hits(tmp_path, api):
    builder, spec, session, _, _ = _fixture(tmp_path, "coding")
    registry = ToolRegistry()
    for name, exposure in (("inspect", ToolExposure.PRELOADED), ("mutate", ToolExposure.DEFERRED)):
        registry.register(ToolSpec(schema=function_tool(name, name, {}), handler=lambda: "unused", exposure=exposure))
    session.metadata["unlocked_tools"] = ["mutate"]
    allowed = registry.schemas_for_turn(session, "coding", agent_spec=spec)
    first = builder.build(session=session, agent_spec=spec, active_turn_start_index=0)
    spec.tool_set = NS(allow=(), deny=("mutate",))
    denied = registry.schemas_for_turn(session, "coding", agent_spec=spec)
    second = builder.build(session=session, agent_spec=spec, active_turn_start_index=0)
    assert second.report.metadata["prefix_cache_hit"] is True
    assert first.messages[0] == second.messages[0]
    first_wire, second_wire = _request(api, first.messages, allowed), _request(api, second.messages, denied)
    assert first_wire["tools"] != second_wire["tools"]
    assert "mutate" not in json.dumps(second_wire["tools"])
    assert not registry.policy.can_execute("mutate", session=session, mode="coding", agent_spec=spec).allowed


def test_hard_reduction_preserves_dynamic_tail_order_and_complete_tool_pair():
    system = {"role": "system", "content": "stable"}
    old = {"role": "user", "content": "old " * 3000}
    latest = {"role": "user", "content": "current request"}
    call = {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "function": {"name": "inspect", "arguments": "{}"}}]}
    result = {"role": "tool", "tool_call_id": "c", "content": "result"}
    state = {"role": "system", "content": "fresh task state", "metadata": {"kind": "task_state_context"}}
    evidence = {"role": "user", "content": "retrieval " * 3000, "metadata": {"kind": "retrieved_evidence_context"}}
    original = [system, old, latest, call, result, state, evidence]
    reduced = reduce_prompt_to_hard_limit(original, max_tokens=200)
    assert reduced == [system, latest, call, result, state]
    assert original[-1] == evidence


def test_required_latest_group_survives_exhausted_budget_for_hard_guard():
    latest = [{"role": "user", "content": "Do not lose the real request"}]
    assert select_complete_groups([latest], max_tokens=0, required_group_indexes=[0]) == latest


def test_coding_budget_reserves_external_fixed_sections_before_tail_selection(tmp_path, monkeypatch):
    import runtime.context.builder as builder_module
    monkeypatch.setattr(builder_module, "DYNAMIC_PROMPT_BUDGET_ENABLED", True)
    builder, spec, session, memory, _ = _fixture(tmp_path, "coding")
    observed = []
    original = builder.coding_context_view_builder

    def capture(session, **kwargs):
        observed.append(copy.deepcopy(kwargs))
        return original(session, **kwargs)

    builder.coding_context_view_builder = capture
    provider = NS(context_window_tokens=8000)
    builder.build(session=session, agent_spec=spec, active_turn_start_index=0,
                  model_provider=provider, reserved_output_tokens=100)
    contribution = ContextContribution(name="workspace-status", content="fixed-observation " * 500)
    second = builder.build(session=session, agent_spec=spec, active_turn_start_index=0,
                           model_provider=provider, reserved_output_tokens=100,
                           contributions=[contribution])
    assert observed[1]["usable_input_tokens"] < observed[0]["usable_input_tokens"]
    assert any(item["content"] == contribution.content for item in observed[1]["static_messages"])
    assert any(memory.text in item["content"] for item in observed[1]["static_messages"])
    assert any(item["content"] == contribution.content for item in second.messages)


def test_hard_reduction_keeps_selected_duplicate_message_at_its_original_occurrence():
    system = {"role": "system", "content": "stable"}
    repeated = {"role": "user", "content": "Continue"}
    obsolete = {"role": "assistant", "content": "obsolete " * 4000}
    middle = [{"role": "user", "content": "Inspect parser"}, {"role": "assistant", "content": "Parser inspected"}]
    latest = {"role": "assistant", "content": "Continuing parser work"}
    state = {"role": "system", "content": "fresh", "metadata": {"kind": "task_state_context"}}
    original = [system, copy.deepcopy(repeated), obsolete, *middle, copy.deepcopy(repeated), latest, state]
    reduced = reduce_prompt_to_hard_limit(original, max_tokens=200)
    assert reduced == [system, *middle, repeated, latest, state]


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        reports = {}
        for mode in ("teammate", "coding"):
            for api in ("chat_completions", "responses"):
                _, _, requests, _ = layout_probe(Path(temp) / (mode + api), mode, api)
                reports[mode + "/" + api] = _prefix_metrics(*requests)
        print(json.dumps(reports, indent=2))
