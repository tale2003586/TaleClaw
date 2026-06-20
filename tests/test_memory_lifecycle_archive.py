import tempfile
import unittest
from pathlib import Path

from models.provider import LLMResponse
from memory.archive_store import ArchivedRecentTurn, MemoryArchiveStore
from memory.history_summary import HistorySummarizer
from memory.lifecycle import MemoryLifecycle
from memory.processor import MemoryProcessingDevice
from memory.store import MemoryStore
from memory.vector_index import MemoryHit
from sessions.session import Session
from postgres_utils import temporary_postgres_schema


class RecordingVectorIndex:
    def __init__(self) -> None:
        self.records = []

    def upsert(self, record) -> None:
        self.records.append(record)

    def search(self, **kwargs):
        return []


class SimilarHistoryVectorIndex(RecordingVectorIndex):
    def search(self, **kwargs):
        return [
            MemoryHit(
                id="turn-1",
                text="user: 我希望项目用 pytest 写测试",
                score=0.82,
                scope=kwargs["scope"],
                source_type="session_turn",
                source_ref="web:default:1",
                metadata={"message_count": 2},
            ),
            MemoryHit(
                id="turn-2",
                text="user: 这个项目测试优先",
                score=0.78,
                scope=kwargs["scope"],
                source_type="session_turn",
                source_ref="web:default:3",
                metadata={"message_count": 2},
            ),
        ]


class StaticCandidateExtractor:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = []

    def extract(self, candidate) -> str:
        self.calls.append(candidate)
        return self.text


class RecordingSummaryProvider:
    def __init__(self, summary: str = "compact assistant summary") -> None:
        self.summary = summary
        self.calls = []

    def chat(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(content=self.summary)


class MemoryLifecycleArchiveTests(unittest.TestCase):
    def test_history_recent_window_and_postgres_archive_share_one_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, temporary_postgres_schema("memory_archive") as dsn:
            root = Path(tmp)
            memory = MemoryStore(root / "memory")
            archive = MemoryArchiveStore(dsn)
            provider = RecordingSummaryProvider()
            lifecycle = MemoryLifecycle(
                memory,
                summarizer=HistorySummarizer(
                    provider=provider,
                    model="test-model",
                    direct_limit=5,
                ),
                archive_store=archive,
                recent_limit=2,
            )
            session = Session(id="web:default")

            for number in range(1, 4):
                session.add_message("user", f"user original text {number}")
                session.add_message("assistant", f"long assistant reply {number}")
                lifecycle.after_turn(session)

            history = memory.read_history()
            recent_turns = memory.read_recent_turns()
            archived_turns = archive.list_recent()

            self.assertIn("USER:\nuser original text 1", history)
            self.assertIn("ASSISTANT_SUMMARY:\ncompact assistant summary", history)
            self.assertEqual(3, len(provider.calls))
            self.assertEqual(2, len(recent_turns))
            self.assertEqual("user original text 2", recent_turns[0]["user_text"])
            self.assertEqual("compact assistant summary", recent_turns[0]["assistant_summary"])
            self.assertEqual(1, len(archived_turns))
            self.assertEqual("user original text 1", archived_turns[0]["user_text"])
            self.assertEqual("compact assistant summary", archived_turns[0]["assistant_summary"])
            self.assertIn("## Turn 2", memory.read_recent_context())

    def test_short_assistant_reply_skips_llm_summary_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "memory")
            provider = RecordingSummaryProvider()
            lifecycle = MemoryLifecycle(
                memory,
                summarizer=HistorySummarizer(
                    provider=provider,
                    model="test-model",
                    direct_limit=20,
                ),
            )
            session = Session(id="web:default")
            session.add_message("user", "keep my full prompt")
            session.add_message("assistant", "done")

            lifecycle.after_turn(session)

            self.assertEqual([], provider.calls)
            self.assertIn("ASSISTANT_SUMMARY:\ndone", memory.read_history())

    def test_recent_markdown_bounds_context_but_json_keeps_full_user_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "memory")
            lifecycle = MemoryLifecycle(memory)
            session = Session(id="web:default")
            user_text = "x" * 1300
            session.add_message("user", user_text)
            session.add_message("assistant", "done")

            lifecycle.after_turn(session)

            self.assertIn(user_text, memory.read_history())
            self.assertEqual(user_text, memory.read_recent_turns()[0]["user_text"])
            self.assertIn("... (100 chars omitted)", memory.read_recent_context())

    def test_recent_context_is_not_in_prompt_memory_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "memory")
            memory.write_recent_context("recent-only detail")
            memory.append("memory", "stable preference")

            prompt_memory = memory.read_all()

            self.assertIn("stable preference", prompt_memory)
            self.assertNotIn("recent-only detail", prompt_memory)
            self.assertIn(
                "recent-only detail",
                memory.read_prompt_memory(include_recent_context=True),
            )

    def test_candidate_memory_promotes_after_repeated_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "memory")
            extractor = StaticCandidateExtractor("这个项目写测试时优先使用 pytest。")
            lifecycle = MemoryLifecycle(
                memory,
                memory_processor=MemoryProcessingDevice(
                    history_vector_index=SimilarHistoryVectorIndex(),
                    scope_resolver=lambda session: "user:test",
                    extractor=extractor,
                    similar_min_hits=2,
                    similar_min_score=0.7,
                ),
                promotion_evidence_count=3,
            )
            session = Session(id="web:default")

            for index in range(3):
                session.add_message("user", f"我希望这个项目用 pytest 写测试 {index}")
                session.add_message("assistant", "done")
                result = lifecycle.after_turn(session)

            candidates = memory.candidates().read()
            self.assertGreaterEqual(result.promoted_count, 1)
            self.assertTrue(any(candidate.status == "promoted" for candidate in candidates))
            self.assertGreaterEqual(len(extractor.calls), 1)
            self.assertIn("这个项目写测试时优先使用 pytest", memory.memory_path.read_text(encoding="utf-8"))
            self.assertIn("pytest", memory.pending_data_path.read_text(encoding="utf-8"))

    def test_candidate_memory_requires_multiple_similar_history_hits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "memory")
            lifecycle = MemoryLifecycle(
                memory,
                memory_processor=MemoryProcessingDevice(
                    history_vector_index=RecordingVectorIndex(),
                    scope_resolver=lambda session: "user:test",
                    similar_min_hits=2,
                ),
            )
            session = Session(id="web:default")
            session.add_message("user", "我希望这个项目用 pytest 写测试")
            session.add_message("assistant", "done")

            result = lifecycle.after_turn(session)

            self.assertEqual(0, result.pending_added)
            self.assertEqual(0, result.candidates_updated)
            self.assertEqual([], memory.candidates().read())

    def test_explicit_memory_writes_stable_memory_not_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "memory")
            lifecycle = MemoryLifecycle(memory)
            session = Session(id="web:default")
            session.add_message("user", "记住我喜欢真实的人物细节")
            session.add_message("assistant", "记住了")

            result = lifecycle.after_turn(session)

            self.assertEqual(1, result.pending_added)
            self.assertEqual([], memory.candidates().read())
            self.assertIn("我喜欢真实的人物细节", memory.memory_path.read_text(encoding="utf-8"))

    def test_lifecycle_indexes_full_session_turn_to_vector_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "memory")
            vector_index = RecordingVectorIndex()
            lifecycle = MemoryLifecycle(
                memory,
                history_vector_index=vector_index,
                scope_resolver=lambda session: "user:test",
            )
            session = Session(id="web:test")
            session.add_message("user", "记住我喜欢真实人物细节")
            session.messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{\"q\":\"人物细节\"}"},
                }],
            })
            session.messages.append({
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "搜索结果",
            })
            session.add_message("assistant", "记住了")

            result = lifecycle.after_turn(session)

            source_types = {record.source_type for record in vector_index.records}
            self.assertGreaterEqual(result.vector_indexed, 1)
            self.assertEqual(0, result.vector_errors)
            self.assertIn("session_turn", source_types)
            self.assertIn("memory_file", source_types)
            record = vector_index.records[0]
            self.assertEqual("user:test", record.scope)
            self.assertEqual(4, record.metadata["message_count"])
            self.assertEqual("call_1", record.metadata["messages"][1]["tool_calls"][0]["id"])
            self.assertEqual("搜索结果", record.metadata["messages"][2]["content"])

    def test_embedding_text_extracts_structured_content_without_json_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lifecycle = MemoryLifecycle(MemoryStore(Path(tmp) / "memory"))
            text = lifecycle._render_messages_for_embedding([
                {
                    "role": "assistant",
                    "content": {"summary": "分析完成", "raw": {"deep": "ignored"}},
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "security_rag_search",
                            "arguments": "{\"query\":\"SQL injection prepared statements\"}",
                        },
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": {"results": [{"text": "Use parameterized queries"}]},
                },
            ])

        self.assertIn("分析完成", text)
        self.assertIn("security_rag_search", text)
        self.assertIn("SQL injection prepared statements", text)
        self.assertIn("Use parameterized queries", text)
        self.assertNotIn('"function"', text)

    def test_archive_ignores_duplicate_source_ref(self) -> None:
        with temporary_postgres_schema("memory_archive_dedupe") as dsn:
            archive = MemoryArchiveStore(dsn)
            turn = ArchivedRecentTurn(
                session_id="web:default",
                mode="hybrid",
                user_text="user",
                assistant_summary="summary",
                source_ref="web:default:1",
                created_at="2026-05-30T00:00:00+00:00",
            )

            self.assertTrue(archive.append(turn))
            self.assertFalse(archive.append(turn))
            self.assertEqual(1, len(archive.list_recent()))


if __name__ == "__main__":
    unittest.main()
