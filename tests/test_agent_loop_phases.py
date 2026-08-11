import asyncio
from types import SimpleNamespace
import unittest

from runtime.messaging.events import InboundMessage
from tests.fakes import make_agent_spec
from applications.turn_coordinator import TurnCoordinator as AgentLoop
from runtime.sessions import Session


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
        self.agent_spec = make_agent_spec(
            name="chat",
            system_prompt="You are helpful.",
            tool_mode="bot",
        )

    def route(self, session, content):
        return SimpleNamespace(
            execution="chat",
            intent="answer",
            agent_spec=self.agent_spec,
            confidence=0.9,
            reason="test",
            switched=self.switched,
            switch_message="switched modes",
        )


class FakeRuntime:
    def __init__(self) -> None:
        self.calls = []

    def run(self, agent, input, context):
        session = context.session
        self.calls.append((session, agent, context.run_state, context.trace_store))
        reply = "runtime reply"
        session.add_message(
            "assistant",
            reply,
            metadata={"run_id": context.run_state.run_id},
        )
        if context.on_text:
            context.on_text(reply)
        return SimpleNamespace(output=reply)


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
        runtime = FakeRuntime()
        plugin_manager = FakePluginManager()
        trace_store = FakeTraceStore()
        emitted = []
        loop = AgentLoop(
            bus,
            sessions,
            runtime,
            FakeRouter(),
            plugin_manager,
            trace_store=trace_store,
        )

        asyncio.run(loop.run_once(on_text=emitted.append))

        self.assertEqual(["runtime reply"], emitted)
        self.assertEqual("runtime reply", bus.outbound[0].content)
        self.assertEqual(["user", "assistant"], [
            message["role"] for message in sessions.session.messages
        ])
        self.assertEqual(1, len(runtime.calls))
        self.assertEqual(1, len(plugin_manager.after_turn_calls))
        self.assertTrue(any(event[0] == "route_selected" for event in trace_store.events))
        self.assertEqual("chat", trace_store.reports[-1]["execution_path"])

    def test_preprocess_abort_finishes_without_runtime(self) -> None:
        inbound = self._inbound()
        bus = FakeBus(inbound)
        sessions = FakeSessions()
        runtime = FakeRuntime()
        loop = AgentLoop(
            bus,
            sessions,
            runtime,
            FakeRouter(),
            FakePluginManager(abort=True),
            trace_store=FakeTraceStore(),
        )

        asyncio.run(loop.run_once())

        self.assertEqual([], runtime.calls)
        self.assertEqual("blocked by plugin", bus.outbound[0].content)
        self.assertEqual([], sessions.session.messages)

    def test_receive_rejects_user_identity_mismatch(self) -> None:
        session = Session(id="web:chat", metadata={"user_id": "existing"})
        loop = AgentLoop(
            FakeBus(),
            FakeSessions(),
            FakeRuntime(),
            FakeRouter(),
        )

        with self.assertRaises(ValueError):
            loop._receive(session, self._inbound(metadata={"user_id": "other"}))


if __name__ == "__main__":
    unittest.main()
