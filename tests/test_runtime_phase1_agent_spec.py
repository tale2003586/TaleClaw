from __future__ import annotations

import pytest

from agents.definitions import BOT_AGENT_SPEC
from agents.definitions import CODING_AGENT_SPEC
from runtime.agent_spec import AgentSpec, RunLimits
from runtime.routing.agent_router import AgentRouter
from runtime.runtime import Runtime
from runtime.sessions import Session


def test_bot_and_coding_are_complete_agent_definitions():
    assert BOT_AGENT_SPEC is BOT_AGENT_SPEC
    assert BOT_AGENT_SPEC.instructions == BOT_AGENT_SPEC.system_prompt
    assert BOT_AGENT_SPEC.model_policy.purpose == "chat"
    assert BOT_AGENT_SPEC.tool_set.mode == "bot"
    assert BOT_AGENT_SPEC.context_policy.name == "chat"
    assert not BOT_AGENT_SPEC.spawn_policy.enabled

    assert CODING_AGENT_SPEC is CODING_AGENT_SPEC
    assert CODING_AGENT_SPEC.instructions == CODING_AGENT_SPEC.system_prompt
    assert CODING_AGENT_SPEC.model_policy.purpose == "coding"
    assert CODING_AGENT_SPEC.tool_set.mode == "coding"
    assert CODING_AGENT_SPEC.context_policy.name == "coding"
    assert CODING_AGENT_SPEC.spawn_policy.enabled
    assert CODING_AGENT_SPEC.spawn_policy.allowed_agent_types == (
        "explore",
        "plan",
        "code",
    )


def test_mode_router_returns_agent_spec_while_preserving_profile_identity():
    session = Session(
        id="phase1:routing",
        active_agent="bot",
        metadata={"user_role": "admin"},
    )

    route = AgentRouter().route(session, "hello")

    assert route.profile is BOT_AGENT_SPEC
    assert route.agent_spec is BOT_AGENT_SPEC
    assert session.metadata["last_route"]["agent"] == "bot"
    assert session.metadata["last_route"]["profile"] == "bot"


def test_agent_spec_compatibility_fields_and_structured_limits_stay_in_sync():
    spec = AgentSpec.from_profile(
        CODING_AGENT_SPEC,
        max_tokens=2048,
        max_reasoning_steps=12,
    )

    assert spec.instructions == CODING_AGENT_SPEC.system_prompt
    assert spec.model_purpose == "coding"
    assert spec.tool_set.mode == "coding"
    assert spec.limits == RunLimits(max_tokens=2048, max_reasoning_steps=12)
    assert spec.max_tokens == 2048
    assert spec.max_reasoning_steps == 12

    changed = spec.with_limits(max_reasoning_steps=20, max_tool_calls=8)
    assert changed.max_tokens == 2048
    assert changed.max_reasoning_steps == 20
    assert changed.limits.max_tool_calls == 8
    assert spec.max_reasoning_steps == 12


def test_agent_spec_rejects_invalid_identity_and_limits():
    with pytest.raises(ValueError, match="name"):
        AgentSpec(name="")
    with pytest.raises(ValueError, match="max_reasoning_steps"):
        RunLimits(max_reasoning_steps=0)


def test_pipeline_prefers_explicit_agent_spec_without_changing_legacy_profile(
    monkeypatch,
):
    captured = {}
    pipeline = object.__new__(Runtime)
    pipeline.agent_runner = type("Runner", (), {
        "run_turn": lambda self, **kwargs: captured.update(kwargs),
    })()
    monkeypatch.setattr(pipeline, "_before_turn", lambda session: None)
    monkeypatch.setattr(pipeline, "_build_context_prefix", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "_after_turn", lambda *args, **kwargs: None)
    session = Session(id="phase1:pipeline")
    session.add_message("user", "hello")

    pipeline._run_turn(
        session,
        BOT_AGENT_SPEC,
        agent_spec=BOT_AGENT_SPEC,
    )

    assert captured["spec"] is BOT_AGENT_SPEC
