import asyncio
import unittest

from runtime.messaging.events import InboundMessage
from runtime.messaging.user_bus import MessageBus
from applications.turn_coordinator import TurnCoordinator as AgentLoop
from runtime.routing.agent_router import AgentRouter
from runtime.sessions.session import Session
from web.server import _is_internal_coding_application


class RecordingSessions:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.saved: list[Session] = []

    def get_or_create(self, session_id: str) -> Session:
        if session_id != self.session.id:
            raise AssertionError(f"unexpected session id: {session_id}")
        return self.session

    def save(self, session: Session) -> None:
        self.saved.append(session)


class RejectingRuntime:
    def run(self, session, agent_spec):
        raise AssertionError("mode switching must not run the runtime")


class RecordingTaskRunner:
    def __init__(self) -> None:
        self.calls = []

    def run_coding_task(
        self,
        *,
        parent_session,
        user_text: str,
        agent_spec,
        workspace_root=None,
        cancel_requested=None,
        on_text=None,
    ) -> str:
        self.calls.append((parent_session, user_text, agent_spec, workspace_root))
        return "coding task completed"


class ModeSwitchTests(unittest.TestCase):
    def test_coding_switch_updates_current_chat_without_starting_task(self) -> None:
        session = Session(id="web:default")
        sessions = RecordingSessions(session)
        task_runner = RecordingTaskRunner()
        bus = MessageBus()
        loop = AgentLoop(
            bus,
            sessions,
            RejectingRuntime(),
            AgentRouter(),
            coding_application=task_runner,
        )

        async def run() -> str:
            await bus.publish_inbound(InboundMessage(
                channel="web",
                chat_id="default",
                sender="user",
                content="/coding",
            ))
            await loop.run_once()
            outbound = await bus._outbound.get()
            return outbound.content

        reply = asyncio.run(run())

        self.assertEqual("已进入编程模式。", reply)
        self.assertEqual("coding", session.active_agent)
        self.assertEqual([], task_runner.calls)
        self.assertEqual(["user", "assistant"], [item["role"] for item in session.messages])
        self.assertEqual("/coding", session.messages[0]["content"])
        self.assertEqual("mode_switch", session.messages[1]["metadata"]["kind"])
        self.assertIs(session, sessions.saved[-1])

    def test_coding_request_runs_isolated_task_from_same_parent_chat(self) -> None:
        session = Session(id="web:default", active_agent="coding")
        sessions = RecordingSessions(session)
        task_runner = RecordingTaskRunner()
        bus = MessageBus()
        loop = AgentLoop(
            bus,
            sessions,
            RejectingRuntime(),
            AgentRouter(),
            coding_application=task_runner,
        )

        async def run() -> str:
            await bus.publish_inbound(InboundMessage(
                channel="web",
                chat_id="default",
                sender="user",
                content="修复当前项目里的 bug",
            ))
            await loop.run_once()
            outbound = await bus._outbound.get()
            return outbound.content

        reply = asyncio.run(run())

        self.assertEqual("coding task completed", reply)
        self.assertEqual(1, len(task_runner.calls))
        self.assertIs(session, task_runner.calls[0][0])
        self.assertEqual(["user", "assistant"], [item["role"] for item in session.messages])

    def test_coding_request_passes_workspace_metadata_to_task_runner(self) -> None:
        session = Session(id="web:default", active_agent="coding")
        sessions = RecordingSessions(session)
        task_runner = RecordingTaskRunner()
        bus = MessageBus()
        loop = AgentLoop(
            bus,
            sessions,
            RejectingRuntime(),
            AgentRouter(),
            coding_application=task_runner,
        )

        async def run() -> None:
            await bus.publish_inbound(InboundMessage(
                channel="web",
                chat_id="default",
                sender="user",
                content="修复当前项目里的 bug",
                metadata={"workspace_root": "/tmp/project-a"},
            ))
            await loop.run_once()
            await bus._outbound.get()

        asyncio.run(run())

        self.assertEqual("/tmp/project-a", task_runner.calls[0][3])

    def test_internal_coding_applications_are_not_regular_web_chats(self) -> None:
        self.assertTrue(_is_internal_coding_application({
            "id": "task:coding-12345678",
            "metadata": {},
        }))
        self.assertTrue(_is_internal_coding_application({
            "id": "legacy-session",
            "metadata": {"kind": "coding_application"},
        }))
        self.assertFalse(_is_internal_coding_application({
            "id": "web:default",
            "metadata": {},
        }))


if __name__ == "__main__":
    unittest.main()
