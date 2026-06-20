import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from models.provider import LLMResponse, OpenAICompatibleProvider
from memory.store import MemoryStore
from sessions.session import Session, SessionManager
from postgres_utils import temporary_postgres_schema
from agents.coding.artifacts import TaskArtifactWriter
from agents.coding.conclusions import (
    ConclusionCandidate,
    ConclusionExtraction,
    TaskConclusionExtractor,
)
from agents.coding.memory_lifecycle import TaskMemoryLifecycle
from agents.coding.promotion import PromotionResult, TaskMemoryPromoter
from agents.coding.runner import TaskSessionRunner
from agents.coding.session import TaskSessionFactory, TaskSessionRecord


class RecordingProvider:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = []

    def chat(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(content=self.content)


class TaskMemoryPromotionTests(unittest.TestCase):
    def test_runner_writes_artifacts_and_promotes_filtered_llm_conclusion(self) -> None:
        class RunnerProvider:
            def __init__(self) -> None:
                self.calls = 0

            def chat(self, **kwargs) -> LLMResponse:
                self.calls += 1
                if self.calls == 1:
                    return LLMResponse(
                        content="Implemented storage persistence.",
                        raw_message={
                            "role": "assistant",
                            "content": "Implemented storage persistence.",
                        },
                    )
                return LLMResponse(content=json.dumps({
                    "summary": "Added persistent storage.",
                    "conclusions": [{
                        "category": "project",
                        "content": "Uploaded files are stored under storage/.",
                        "evidence": "docker-compose.yml",
                        "confidence": 0.96,
                    }],
                }))

        class RunnerTools:
            def reset_turn_unlocks(self, session) -> None:
                session.metadata["unlocked_tools"] = []

            def schemas_for_turn(self, session, mode):
                return []

        with tempfile.TemporaryDirectory() as tmp, temporary_postgres_schema("task_memory") as dsn:
            root = Path(tmp)
            sessions = SessionManager(dsn)
            provider = RunnerProvider()
            base_pipeline = SimpleNamespace(
                tools=RunnerTools(),
                provider=provider,
                model="test-model",
                tool_executor=object(),
                max_tokens=8000,
            )
            runner = TaskSessionRunner(
                sessions=sessions,
                base_pipeline=base_pipeline,
                global_memory=MemoryStore(root / "memory"),
            )
            runner.factory = TaskSessionFactory(sessions, root=root / ".task_sessions")
            try:
                reply = runner.run_coding_task(
                    parent_session=Session(id="web:default"),
                    user_text="Add storage persistence",
                    profile=SimpleNamespace(tool_mode="coding", system_prompt="coding"),
                )
            finally:
                sessions.close()

            task_dirs = list((root / ".task_sessions").iterdir())
            self.assertEqual(1, len(task_dirs))
            self.assertTrue((task_dirs[0] / "TASK_LOG.md").exists())
            self.assertTrue((task_dirs[0] / "CONCLUSIONS.json").exists())
            self.assertIn("Uploaded files are stored under storage/.", runner.global_memory.read_pending())
            self.assertIn("Task log:", reply)

    def test_task_lifecycle_keeps_wrapped_prompt_out_of_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "memory")
            session = Session(id="task:coding-12345678", current_mode="coding")
            session.add_message(
                "user",
                "<task-session>\n我希望这里使用 pytest\n</task-session>",
            )
            session.add_message("assistant", "done")

            TaskMemoryLifecycle(memory).after_turn(session)

            self.assertEqual("# Pending Memory\n\n", memory.read_pending())
            self.assertIn("我希望这里使用 pytest", memory.read_history())

    def test_promoter_keeps_reusable_candidates_and_rejects_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            global_memory = MemoryStore(root / "global")
            task_memory = MemoryStore(root / "task")
            task_memory.pending_path.write_text(
                "# Pending Memory\n\n"
                "- [project] Docker Compose mounts storage/ to /app/storage.\n"
                "- Task recent context: latest_user: wrapped prompt\n"
                "- <task-session>temporary wrapper</task-session>\n",
                encoding="utf-8",
            )
            extracted = [
                ConclusionCandidate(
                    category="decision",
                    content="The web server keeps uploaded files in storage/.",
                    evidence="web/server.py",
                    confidence=0.94,
                ),
                ConclusionCandidate(
                    category="fact",
                    content="Temporary thought",
                    confidence=0.3,
                ),
                ConclusionCandidate(
                    category="project",
                    content="Docker Compose mounts storage/ to /app/storage.",
                    confidence=0.98,
                ),
            ]

            result = TaskMemoryPromoter(global_memory).promote(
                task_id="coding-12345678",
                task_memory=task_memory,
                extracted_conclusions=extracted,
            )
            pending = global_memory.read_pending()

            self.assertEqual(2, len(result.promoted))
            self.assertGreaterEqual(len(result.rejected), 3)
            self.assertIn("Docker Compose mounts storage/ to /app/storage.", pending)
            self.assertIn("The web server keeps uploaded files in storage/.", pending)
            self.assertNotIn("Task recent context", pending)
            self.assertNotIn("<task-session>", pending)
            self.assertNotIn("Temporary thought", pending)

    def test_extractor_parses_structured_json(self) -> None:
        provider = RecordingProvider(
            "```json\n"
            '{"summary":"Added storage persistence.",'
            '"conclusions":[{"category":"project","content":"Files live in storage/.",'
            '"evidence":"docker-compose.yml","confidence":0.91}]}\n'
            "```"
        )

        result = TaskConclusionExtractor(provider=provider, model="test-model").extract(
            user_request="Add storage",
            task_summary="Done",
            messages=[],
        )

        self.assertEqual("Added storage persistence.", result.summary)
        self.assertEqual("Files live in storage/.", result.candidates[0].content)
        self.assertEqual([], provider.calls[0]["tools"])
        self.assertEqual("none", provider.calls[0]["tool_choice"])

    def test_artifact_writer_persists_log_and_conclusions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_root = Path(tmp) / "coding-12345678"
            memory_root = task_root / "memory"
            session = Session(
                id="task:coding-12345678",
                current_mode="coding",
                metadata={"status": "completed"},
            )
            session.add_message("user", "Fix the bug")
            session.add_message(
                "tool",
                "1 passed",
                status="success",
                final_arguments={"command": "pytest"},
            )
            session.add_message("assistant", "Fixed")
            record = TaskSessionRecord(
                session=session,
                task_id="coding-12345678",
                parent_session_id="web:default",
                task_type="coding",
                memory_root=memory_root,
            )
            candidate = ConclusionCandidate(
                category="project",
                content="Tests use pytest.",
                evidence="pytest",
                confidence=0.95,
            )

            paths = TaskArtifactWriter().write(
                record=record,
                user_request="Fix the bug",
                task_reply="Fixed",
                extraction=ConclusionExtraction(
                    summary="Fixed and tested the bug.",
                    candidates=[candidate],
                ),
                promotion=PromotionResult(promoted=[candidate]),
            )

            log = paths.task_log_path.read_text(encoding="utf-8")
            conclusions = json.loads(paths.conclusions_path.read_text(encoding="utf-8"))
            self.assertIn("# Task Log: coding-12345678", log)
            self.assertIn('"command": "pytest"', log)
            self.assertEqual("Fixed and tested the bug.", conclusions["summary"])
            self.assertEqual("Tests use pytest.", conclusions["promoted"][0]["content"])


class ProviderRequestTests(unittest.TestCase):
    def test_provider_omits_empty_tool_list_for_summary_calls(self) -> None:
        class Message:
            content = "{}"
            tool_calls = []

            def model_dump(self, exclude_none=True):
                return {"role": "assistant", "content": self.content}

        class Completions:
            def __init__(self) -> None:
                self.kwargs = None

            def create(self, **kwargs):
                self.kwargs = kwargs
                return type("Response", (), {
                    "choices": [type("Choice", (), {"message": Message()})()],
                })()

        completions = Completions()
        client = type("Client", (), {
            "chat": type("Chat", (), {"completions": completions})(),
        })()

        OpenAICompatibleProvider(client).chat(
            model="test-model",
            messages=[{"role": "user", "content": "summarize"}],
            tools=[],
            tool_choice="none",
            max_tokens=100,
        )

        self.assertNotIn("tools", completions.kwargs)
        self.assertNotIn("tool_choice", completions.kwargs)


if __name__ == "__main__":
    unittest.main()
