import asyncio
from types import SimpleNamespace
import unittest

from bus.events import InboundMessage
from modes.base import ModeProfile
from runtime.agent_loop import AgentLoop
from sessions import Session


class FakeBus:
    def __init__(self, inbound=None) -> None:
        self.inbound = inbound
        self.outbound = []

    async def consume_inbound(self):
        return self.inbound

    async def publish_outbound(self, message):
        self.outbound.append(message)


class FakeSessions:
    def __init__(self) -> None:
        self.session = Session(id="web:chat")
        self.saved = []

    def get_or_create(self, session_id):
        self.session.id = session_id
        return self.session

    def save(self, session):
        self.saved.append(session)


class FakeTraceStore:
    def __init__(self) -> None:
        self.events = []
        self.started = []
        self.states = []
        self.reports = []

    def start_run(self, run_state):
        self.started.append(run_state.run_id)

    def append_event(self, run_state, event_name, payload, **kwargs):
        self.events.append((event_name, payload, kwargs))

    def write_run_state(self, run_state):
        self.states.append(run_state.run_id)

    def write_report(self, run_state, report):
        self.reports.append(report)

    def run_dir(self, run_state):
        return None


class FakePluginManager:
    def __init__(self, *, abort=False) -> None:
        self.abort = abort
        self.after_turn_calls = []
        self.after_run_calls = []

    def before_turn(self, inbound, session):
        return SimpleNamespace(abort=self.abort, reply="blocked by plugin")

    def after_turn(self, inbound, session, reply):
        self.after_turn_calls.append((inbound, session, reply))

    def after_run(self, **kwargs):
        self.after_run_calls.append(kwargs)


class FakeRouter:
    def __init__(self, *, switched=False) -> None:
        self.switched = switched
        self.profile = ModeProfile(
            name="chat",
            system_prompt="You are helpful.",
            tool_mode="bot",
        )

    def route(self, session, content):
        return SimpleNamespace(
            execution="chat",
            intent="answer",
            profile=self.profile,
            confidence=0.9,
            reason="test",
            switched=self.switched,
            switch_message="switched modes",
        )


class FakePipeline:
    def __init__(self) -> None:
        self.calls = []

    def run(self, session, profile, on_text=None, run_state=None, trace_store=None):
        self.calls.append((session, profile, run_state, trace_store))
        reply = "pipeline reply"
        session.add_message("assistant", reply, metadata={"run_id": run_state.run_id})
        if on_text:
            on_text(reply)
        return reply


class AgentLoopPhaseTests(unittest.TestCase):
    def _inbound(self, *, metadata=None):
        return InboundMessage(
            channel="web",
            chat_id="chat",
            sender="user",
            content="hello",
            metadata=metadata or {"user_id": "u1", "user_role": "admin"},
        )

    def test_run_once_flows_through_route_record_execute_deliver(self) -> None:
        inbound = self._inbound()
        bus = FakeBus(inbound)
        sessions = FakeSessions()
        pipeline = FakePipeline()
        plugin_manager = FakePluginManager()
        trace_store = FakeTraceStore()
        emitted = []
        loop = AgentLoop(
            bus,
            sessions,
            pipeline,
            FakeRouter(),
            plugin_manager,
            trace_store=trace_store,
        )

        asyncio.run(loop.run_once(on_text=emitted.append))

        self.assertEqual(["pipeline reply"], emitted)
        self.assertEqual("pipeline reply", bus.outbound[0].content)
        self.assertEqual(["user", "assistant"], [
            message["role"] for message in sessions.session.messages
        ])
        self.assertEqual(1, len(pipeline.calls))
        self.assertEqual(1, len(plugin_manager.after_turn_calls))
        self.assertTrue(any(event[0] == "route_selected" for event in trace_store.events))
        self.assertEqual("chat", trace_store.reports[-1]["execution_path"])

    def test_preprocess_abort_finishes_without_pipeline(self) -> None:
        inbound = self._inbound()
        bus = FakeBus(inbound)
        sessions = FakeSessions()
        pipeline = FakePipeline()
        loop = AgentLoop(
            bus,
            sessions,
            pipeline,
            FakeRouter(),
            FakePluginManager(abort=True),
            trace_store=FakeTraceStore(),
        )

        asyncio.run(loop.run_once())

        self.assertEqual([], pipeline.calls)
        self.assertEqual("blocked by plugin", bus.outbound[0].content)
        self.assertEqual([], sessions.session.messages)

    def test_receive_rejects_user_identity_mismatch(self) -> None:
        session = Session(id="web:chat", metadata={"user_id": "existing"})
        loop = AgentLoop(
            FakeBus(),
            FakeSessions(),
            FakePipeline(),
            FakeRouter(),
        )

        with self.assertRaises(ValueError):
            loop._receive(session, self._inbound(metadata={"user_id": "other"}))


if __name__ == "__main__":
    unittest.main()
