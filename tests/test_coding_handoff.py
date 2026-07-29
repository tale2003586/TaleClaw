import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from applications.coding.runner import CodingApplication
from applications.coding.session import TaskSessionFactory
from memory.store import MemoryStore
from models.provider import LLMResponse
from runtime.agent_loop import AgentLoop
from applications.coding.handoff import (
    CODING_HANDOFF_METADATA_KEY,
    CODING_TASK_SUMMARY_METADATA_KEY,
    PENDING_CODING_TASK_SUMMARY_METADATA_KEY,
    build_coding_session_handoff,
)
from runtime.sessions.session import Session


class CodingSessionHandoffTests(unittest.TestCase):
    def test_handoff_keeps_two_recent_completed_turns_and_summarizes_prior(self) -> None:
        session = Session(id="web:default")
        session.add_message("user", "Earlier: what is the task handoff design?")
        session.add_message("assistant", "It passes compact parent conversation context.")
        session.add_message("user", "What is a retry budget?")
        session.add_message("assistant", "A retry budget limits repeated model/tool attempts.")
        session.add_message("user", "What is a checkpoint?")
        session.add_message("assistant", "A checkpoint records enough state to resume work.")
        session.add_message("user", "把这个加入到 coding 任务里")

        handoff = build_coding_session_handoff(
            session,
            current_user_request="把这个加入到 coding 任务里",
        )

        self.assertEqual(2, len(handoff.recent_turns))
        self.assertEqual("What is a retry budget?", handoff.recent_turns[0].user_original)
        self.assertEqual("What is a checkpoint?", handoff.recent_turns[1].user_original)
        self.assertIn("Earlier: what is the task handoff design?", handoff.prior_summary)
        self.assertNotIn("把这个加入到 coding 任务里", handoff.recent_turns[1].user_original)

        rendered = handoff.render_prompt_block()
        self.assertIn("<conversation-history-handoff>", rendered)
        self.assertNotIn("<current-user-request>", rendered)
        self.assertNotIn("把这个加入到 coding 任务里", rendered)
        self.assertIn("A retry budget limits", rendered)

    def test_handoff_prefers_coding_task_summary_metadata_for_assistant_reply(self) -> None:
        session = Session(id="web:default")
        session.add_message("user", "Add the storage feature")
        session.add_message(
            "assistant",
            "Long visible task transcript with logs and artifact paths.",
            metadata={
                CODING_TASK_SUMMARY_METADATA_KEY: {
                    "task_id": "coding-12345678",
                    "status": "completed",
                    "summary": "Implemented storage persistence.",
                    "user_request": "Add the storage feature",
                }
            },
        )
        session.add_message("user", "现在把它扩展到上传流程")

        handoff = build_coding_session_handoff(
            session,
            current_user_request="现在把它扩展到上传流程",
        )

        self.assertEqual(1, len(handoff.recent_turns))
        self.assertIn(
            "Implemented storage persistence.",
            handoff.recent_turns[0].assistant_summary,
        )
        self.assertNotIn(
            "Long visible task transcript",
            handoff.recent_turns[0].assistant_summary,
        )

    def test_coding_application_injects_handoff_into_coding_prompt(self) -> None:
        class InMemorySessions:
            def __init__(self) -> None:
                self.sessions = {}

            def get_or_create(self, session_id):
                if session_id not in self.sessions:
                    self.sessions[session_id] = Session(id=session_id)
                return self.sessions[session_id]

            def save(self, session) -> None:
                self.sessions[session.id] = session

        class RunnerProvider:
            def __init__(self) -> None:
                self.calls = []

            def chat(self, **kwargs) -> LLMResponse:
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    return LLMResponse(
                        content="Implemented the requested change.",
                        raw_message={
                            "role": "assistant",
                            "content": "Implemented the requested change.",
                        },
                    )
                return LLMResponse(
                    content='{"summary":"Completed the requested change.","conclusions":[]}'
                )

        class RunnerTools:
            def reset_turn_unlocks(self, session) -> None:
                session.metadata["unlocked_tools"] = []

            def schemas_for_turn(self, session, mode):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = InMemorySessions()
            provider = RunnerProvider()
            runner = CodingApplication(
                sessions=sessions,
                base_pipeline=SimpleNamespace(
                    tools=RunnerTools(),
                    provider=provider,
                    model="test-model",
                    tool_executor=object(),
                    max_tokens=8000,
                ),
                global_memory=MemoryStore(root / "memory"),
                workspace_root=root,
            )
            runner.factory = TaskSessionFactory(sessions, root=root / ".coding_applications")

            parent = Session(id="web:default", active_agent="coding")
            parent.add_message("user", "What is the checkpoint idea?")
            parent.add_message("assistant", "A checkpoint records resumable task state.")
            parent.add_message("user", "把它接入 coding 任务")

            runner.run_coding_task(
                parent_session=parent,
                user_text="把它接入 coding 任务",
                profile=SimpleNamespace(tool_mode="coding", system_prompt="coding"),
            )

            first_prompt = "\n".join(
                str(message.get("content") or "")
                for message in provider.calls[0]["messages"]
            )
            coding_application = next(
                session
                for session_id, session in sessions.sessions.items()
                if session_id.startswith("task:")
            )

            self.assertIn("<conversation-history-handoff>", first_prompt)
            self.assertIn("What is the checkpoint idea?", first_prompt)
            self.assertIn("A checkpoint records resumable task state.", first_prompt)
            self.assertIn(CODING_HANDOFF_METADATA_KEY, coding_application.metadata)
            self.assertIn(PENDING_CODING_TASK_SUMMARY_METADATA_KEY, parent.metadata)

    def test_agent_loop_attaches_pending_coding_summary_to_parent_reply(self) -> None:
        class FakeCodingApplication:
            def run_coding_task(
                self,
                *,
                parent_session,
                user_text,
                profile,
                cancel_requested=None,
            ) -> str:
                parent_session.metadata[PENDING_CODING_TASK_SUMMARY_METADATA_KEY] = {
                    "task_id": "coding-abcdef12",
                    "status": "completed",
                    "summary": "Changed the requested files.",
                    "user_request": user_text,
                }
                return "Visible coding reply."

        loop = AgentLoop(
            bus=None,
            sessions=None,
            pipeline=None,
            router=None,
            coding_application=FakeCodingApplication(),
        )
        session = Session(id="web:default", active_agent="coding")
        inbound = SimpleNamespace(content="Change the files", metadata={})
        route = SimpleNamespace(profile=SimpleNamespace(tool_mode="coding"))
        run_state = SimpleNamespace(run_id="run-1")

        reply = loop._execute(session, inbound, route, run_state, on_text=None)

        self.assertEqual("Visible coding reply.", reply)
        metadata = session.messages[-1]["metadata"]
        self.assertEqual("coding_task_result", metadata["kind"])
        self.assertEqual(
            "coding-abcdef12",
            metadata[CODING_TASK_SUMMARY_METADATA_KEY]["task_id"],
        )
        self.assertNotIn(PENDING_CODING_TASK_SUMMARY_METADATA_KEY, session.metadata)


if __name__ == "__main__":
    unittest.main()
