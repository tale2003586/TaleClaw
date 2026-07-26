from __future__ import annotations

import asyncio
from types import SimpleNamespace

from runtime.messaging.events import InboundMessage
from runtime.messaging.user_bus import MessageBus
from tests.fakes import make_agent_spec
from runtime.agent_loop import AgentLoop
from runtime.sessions import Session


class Sessions:
    def __init__(self):
        self.session = Session(id="unset")
        self.saved = []

    def get_or_create(self, session_id):
        self.session.id = session_id
        return self.session

    def save(self, session):
        self.saved.append(session)


class Router:
    def route(self, session, text):
        return SimpleNamespace(
            profile=make_agent_spec("chat", "chat prompt", "bot"),
            switched=False,
            switch_message=None,
            intent="chat",
            execution="runtime",
            confidence=1.0,
            reason="phase0",
        )


class Runtime:
    def __init__(self):
        self.calls = []

    def run(self, agent, input, context):
        session = context.session
        self.calls.append((session.id, agent.name))
        session.add_message("assistant", "reply")
        callback = context.on_text
        if callback:
            callback("reply")
        return SimpleNamespace(output="reply")


def test_default_single_agent_run_once_consumes_and_publishes_user_bus():
    async def scenario():
        bus = MessageBus()
        sessions = Sessions()
        pipeline = Runtime()
        loop = AgentLoop(bus, sessions, pipeline, Router())
        await bus.publish_inbound(InboundMessage(
            channel="web",
            chat_id="phase0",
            sender="user",
            content="hello",
            metadata={"user_id": "u1", "user_role": "admin"},
        ))
        await loop.run_once()
        outbound = await asyncio.wait_for(bus._outbound.get(), timeout=0.1)
        return sessions, pipeline, outbound

    sessions, pipeline, outbound = asyncio.run(scenario())

    assert pipeline.calls == [("web:phase0", "chat")]
    assert [item["role"] for item in sessions.session.messages] == ["user", "assistant"]
    assert outbound.content == "reply"
    # The real AgentRouter writes last_route; this intentionally minimal fake router does not.
    assert sessions.session.metadata["user_id"] == "u1"
    assert sessions.session.metadata["user_role"] == "admin"
    assert "active_run_id" not in sessions.session.metadata
    assert "last_run_id" in sessions.session.metadata


def test_run_inbound_bypasses_inbound_queue_but_still_publishes_outbound():
    async def scenario():
        bus = MessageBus()
        pipeline = Runtime()
        loop = AgentLoop(bus, Sessions(), pipeline, Router())
        await loop.run_inbound(InboundMessage(
            channel="cli",
            chat_id="local",
            sender="user",
            content="hello",
        ))
        assert bus._inbound.empty()
        return await asyncio.wait_for(bus._outbound.get(), timeout=0.1)

    assert asyncio.run(scenario()).content == "reply"
