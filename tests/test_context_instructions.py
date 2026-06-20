import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from runtime.context import ContextBuilder
from memory.vector_index import MemoryHit
from sessions.session import Session


class MemoryStore:
    def __init__(self, text: str) -> None:
        self.text = text

    def read_all(self) -> str:
        return self.text


class FakeVectorIndex:
    def search(self, **kwargs):
        return [
            MemoryHit(
                id="hit-1",
                text="user: 之前讨论 trace 字段\nassistant.tool_call: read_file trace.jsonl\ntool_result[call_1]: model_requested 字段内容",
                score=0.91,
                scope=kwargs["scope"],
                source_type="session_turn",
                source_ref="web:test:3",
                metadata={"message_count": 3},
            )
        ]

    def upsert(self, record) -> None:
        return None


class ContextInstructionTests(unittest.TestCase):
    def test_bot_profile_loads_assistant_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agent").mkdir()
            (root / ".agent" / "assistant.md").write_text(
                "assistant rules",
                encoding="utf-8",
            )
            (root / ".agent" / "coding.md").write_text(
                "coding rules",
                encoding="utf-8",
            )
            context = ContextBuilder(instruction_root=root).build(
                session=Session(id="web:test"),
                profile=SimpleNamespace(
                    system_prompt="base",
                    tool_mode="bot",
                ),
            )

            system = context.messages[0]["content"]
            self.assertIn("assistant rules", system)
            self.assertNotIn("coding rules", system)
            report = context.report.to_dict()
            self.assertIn("mode_instructions", report["sections"])
            self.assertNotIn("project_instructions", report["sections"])
            self.assertEqual(
                [".agent/assistant.md"],
                report["sections"]["mode_instructions"]["metadata"]["sources"],
            )
            self.assertGreater(report["total_chars"], 0)

    def test_coding_profile_loads_coding_and_project_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agent").mkdir()
            (root / ".agent" / "assistant.md").write_text(
                "assistant rules",
                encoding="utf-8",
            )
            (root / ".agent" / "coding.md").write_text(
                "coding rules",
                encoding="utf-8",
            )
            (root / "AGENTS.md").write_text(
                "project rules",
                encoding="utf-8",
            )
            context = ContextBuilder(instruction_root=root).build(
                session=Session(id="task:test"),
                profile=SimpleNamespace(
                    system_prompt="base",
                    tool_mode="coding",
                ),
            )

            system = context.messages[0]["content"]
            self.assertIn("coding rules", system)
            self.assertIn("project rules", system)
            self.assertNotIn("assistant rules", system)
            report = context.report.to_dict()
            self.assertIn("mode_instructions", report["sections"])
            self.assertIn("project_instructions", report["sections"])
            self.assertEqual(
                [".agent/coding.md"],
                report["sections"]["mode_instructions"]["metadata"]["sources"],
            )
            self.assertEqual(
                ["AGENTS.md"],
                report["sections"]["project_instructions"]["metadata"]["sources"],
            )

    def test_instruction_report_marks_truncated_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agent").mkdir()
            (root / ".agent" / "assistant.md").write_text(
                "x" * 1200,
                encoding="utf-8",
            )
            context = ContextBuilder(
                instruction_root=root,
                instruction_limit=50,
            ).build(
                session=Session(id="web:test"),
                profile=SimpleNamespace(
                    system_prompt="base",
                    tool_mode="bot",
                ),
            )

            section = context.report.to_dict()["sections"]["mode_instructions"]
            self.assertTrue(section["truncated"])
            self.assertEqual(1200, section["raw_chars"])
            self.assertGreater(section["rendered_chars"], 1000)

    def test_report_splits_current_request_from_history(self) -> None:
        session = Session(id="web:test")
        session.add_message("user", "old question")
        session.add_message("assistant", "old answer")
        session.add_message("user", "new question")

        context = ContextBuilder().build(
            session=session,
            profile=SimpleNamespace(
                system_prompt="base",
                tool_mode="bot",
            ),
        )

        sections = context.report.to_dict()["sections"]
        self.assertEqual(len("new question"), sections["current_request"]["raw_chars"])
        self.assertEqual(len("new question"), sections["current_request"]["rendered_chars"])
        self.assertTrue(sections["current_request"]["metadata"]["preserve"])
        self.assertEqual(2, sections["conversation_history"]["metadata"]["message_count"])

    def test_section_budget_trims_optional_sections_and_preserves_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agent").mkdir()
            (root / ".agent" / "coding.md").write_text(
                "coding-rule-" * 80,
                encoding="utf-8",
            )
            (root / "AGENTS.md").write_text(
                "project-rule-" * 80,
                encoding="utf-8",
            )
            session = Session(id="task:test")
            session.add_message("user", "please keep this exact current request")

            with patch.dict(
                "os.environ",
                {
                    "CONTEXT_ENABLE_SECTION_BUDGET": "1",
                    "CONTEXT_BUDGET_CHARS": "1000",
                    "CONTEXT_MODE_INSTRUCTIONS_BUDGET": "80",
                    "CONTEXT_PROJECT_INSTRUCTIONS_BUDGET": "80",
                    "CONTEXT_MEMORY_BUDGET": "90",
                    "CONTEXT_TASK_RUNTIME_EVENTS_BUDGET": "90",
                    "CONTEXT_MODE_INSTRUCTIONS_FLOOR": "40",
                    "CONTEXT_PROJECT_INSTRUCTIONS_FLOOR": "40",
                    "CONTEXT_MEMORY_FLOOR": "40",
                    "CONTEXT_TASK_RUNTIME_EVENTS_FLOOR": "40",
                },
                clear=False,
            ):
                context = ContextBuilder(
                    memory_store=MemoryStore("memory-item-" * 80),
                    instruction_root=root,
                ).build(
                    session=session,
                    profile=SimpleNamespace(
                        system_prompt="base",
                        tool_mode="coding",
                    ),
                    inbox=[{"from": "alice", "body": "inbox-item-" * 80}],
                    background_results=[
                        {
                            "task_id": "bg1",
                            "status": "done",
                            "result": "background-result-" * 80,
                        }
                    ],
                )

            report = context.report.to_dict()
            sections = report["sections"]
            self.assertEqual("please keep this exact current request", context.messages[-1]["content"])
            self.assertEqual(
                len("please keep this exact current request"),
                sections["current_request"]["rendered_chars"],
            )
            self.assertTrue(sections["mode_instructions"]["truncated"])
            self.assertTrue(sections["project_instructions"]["truncated"])
            self.assertTrue(sections["memory"]["truncated"])
            self.assertTrue(sections["task_runtime_events"]["truncated"])
            self.assertLessEqual(sections["mode_instructions"]["rendered_chars"], 80)
            self.assertLessEqual(sections["project_instructions"]["rendered_chars"], 80)
            self.assertLessEqual(sections["memory"]["rendered_chars"], 90)
            self.assertLessEqual(sections["task_runtime_events"]["rendered_chars"], 90)
            self.assertEqual(
                {
                    "memory",
                    "mode_instructions",
                    "project_instructions",
                    "task_runtime_events",
                },
                {item["section"] for item in report["reductions"]},
            )
            self.assertIn("...[truncated]", context.messages[0]["content"])
            self.assertIn("...[truncated]", context.messages[-2]["content"])
            self.assertTrue(report["metadata"]["section_budget_enabled"])

    def test_context_frame_is_inserted_before_current_request(self) -> None:
        session = Session(id="web:test")
        session.add_message("user", "old request")
        session.add_message("assistant", "old answer")
        session.add_message("user", "this is the actual current request")

        context = ContextBuilder(
            memory_store=MemoryStore("remembered preference"),
        ).build(
            session=session,
            profile=SimpleNamespace(
                system_prompt="base",
                tool_mode="bot",
            ),
        )

        self.assertIn("<memory>", context.messages[-2]["content"])
        self.assertEqual("this is the actual current request", context.messages[-1]["content"])

    def test_conversation_history_budget_summarizes_middle_and_preserves_request(self) -> None:
        session = Session(id="web:test")
        for index in range(8):
            session.add_message("user", f"old question {index} " + ("x" * 120))
            session.add_message("assistant", f"old answer {index} " + ("y" * 120))
        session.add_message("user", "current request must stay intact")

        with patch.dict(
            "os.environ",
            {
                "CONTEXT_ENABLE_SECTION_BUDGET": "1",
                "CONTEXT_BUDGET_CHARS": "1000",
                "CONTEXT_CONVERSATION_HISTORY_BUDGET": "500",
                "CONTEXT_CONVERSATION_HISTORY_STRATEGY": "summary_middle",
                "CONTEXT_HISTORY_KEEP_HEAD_TURNS": "1",
                "CONTEXT_HISTORY_KEEP_TAIL_TURNS": "1",
                "CONTEXT_HISTORY_SUMMARY_MAX_CHARS": "350",
            },
            clear=False,
        ):
            context = ContextBuilder().build(
                session=session,
                profile=SimpleNamespace(
                    system_prompt="base",
                    tool_mode="bot",
                ),
            )

        report = context.report.to_dict()
        history_section = report["sections"]["conversation_history"]

        self.assertTrue(history_section["truncated"])
        self.assertEqual(500, history_section["budget_chars"])
        self.assertEqual("summary_middle", history_section["metadata"]["strategy"])
        self.assertGreater(history_section["metadata"]["summarized_message_count"], 0)
        self.assertIn(
            "Conversation history summary",
            "\n".join(str(message.get("content", "")) for message in context.messages),
        )
        self.assertEqual("current request must stay intact", context.messages[-1]["content"])
        self.assertLess(history_section["rendered_chars"], history_section["raw_chars"])
        self.assertIn("conversation_history", {item["section"] for item in report["reductions"]})

    def test_conversation_history_budget_keeps_tool_call_pairs_together(self) -> None:
        session = Session(id="web:test")
        session.add_message("user", "old setup " + ("x" * 300))
        session.messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_old",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{\"path\":\"old.py\"}"},
            }],
        })
        session.messages.append({
            "role": "tool",
            "tool_call_id": "call_old",
            "content": "old output " + ("z" * 300),
        })
        session.add_message("user", "recent setup")
        session.messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_recent",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{\"path\":\"recent.py\"}"},
            }],
        })
        session.messages.append({
            "role": "tool",
            "tool_call_id": "call_recent",
            "content": "recent output",
        })
        session.add_message("user", "current request")

        with patch.dict(
            "os.environ",
            {
                "CONTEXT_ENABLE_SECTION_BUDGET": "1",
                "CONTEXT_CONVERSATION_HISTORY_BUDGET": "450",
                "CONTEXT_CONVERSATION_HISTORY_STRATEGY": "summary_middle",
                "CONTEXT_HISTORY_KEEP_HEAD_TURNS": "0",
                "CONTEXT_HISTORY_KEEP_TAIL_TURNS": "1",
                "CONTEXT_HISTORY_SUMMARY_MAX_CHARS": "250",
            },
            clear=False,
        ):
            context = ContextBuilder().build(
                session=session,
                profile=SimpleNamespace(
                    system_prompt="base",
                    tool_mode="bot",
                ),
            )

        rendered = context.messages
        recent_assistant_index = next(
            index
            for index, message in enumerate(rendered)
            if message.get("role") == "assistant"
            and (message.get("tool_calls") or [{}])[0].get("id") == "call_recent"
        )
        self.assertEqual("tool", rendered[recent_assistant_index + 1]["role"])
        self.assertEqual("call_recent", rendered[recent_assistant_index + 1]["tool_call_id"])
        self.assertEqual("current request", rendered[-1]["content"])

    def test_active_turn_preserves_user_assistant_tool_order_across_reasoning_steps(self) -> None:
        session = Session(id="web:test")
        session.add_message("user", "old task " + ("x" * 300))
        session.add_message("assistant", "old answer " + ("y" * 300))
        active_turn_start = len(session.messages)
        session.add_message("user", "fix the current bug")
        session.messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_current",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{\"path\":\"bug.py\"}"},
            }],
        })
        session.messages.append({
            "role": "tool",
            "tool_call_id": "call_current",
            "content": "very long tool output " + ("z" * 500),
        })

        with patch.dict(
            "os.environ",
            {
                "CONTEXT_ENABLE_SECTION_BUDGET": "1",
                "CONTEXT_CONVERSATION_HISTORY_BUDGET": "300",
                "CONTEXT_CONVERSATION_HISTORY_STRATEGY": "summary_middle",
                "CONTEXT_HISTORY_KEEP_HEAD_TURNS": "0",
                "CONTEXT_HISTORY_KEEP_TAIL_TURNS": "0",
                "CONTEXT_HISTORY_SUMMARY_MAX_CHARS": "180",
            },
            clear=False,
        ):
            context = ContextBuilder(
                memory_store=MemoryStore("memory before active turn"),
            ).build(
                session=session,
                profile=SimpleNamespace(
                    system_prompt="base",
                    tool_mode="bot",
                ),
                active_turn_start_index=active_turn_start,
            )

        active_messages = context.messages[-3:]
        self.assertEqual(["user", "assistant", "tool"], [m["role"] for m in active_messages])
        self.assertEqual("fix the current bug", active_messages[0]["content"])
        self.assertEqual("call_current", active_messages[1]["tool_calls"][0]["id"])
        self.assertEqual("call_current", active_messages[2]["tool_call_id"])
        sections = context.report.to_dict()["sections"]
        self.assertEqual(active_turn_start, sections["active_turn"]["metadata"]["start_index"])
        self.assertEqual(3, sections["active_turn"]["metadata"]["message_count"])
        self.assertEqual(len("fix the current bug"), sections["current_request"]["raw_chars"])

    def test_active_turn_budget_summarizes_older_tool_results(self) -> None:
        session = Session(id="web:test")
        active_turn_start = len(session.messages)
        session.add_message("user", "search and then answer")
        for index in range(3):
            call_id = f"call_{index}"
            session.messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": f"{{\"query\":\"topic {index}\"}}",
                    },
                }],
            })
            session.messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": f"search output {index} " + ("z" * 900),
            })

        with patch.dict(
            "os.environ",
            {
                "CONTEXT_ENABLE_SECTION_BUDGET": "1",
                "CONTEXT_ACTIVE_TURN_BUDGET": "900",
                "CONTEXT_ACTIVE_TURN_FLOOR": "500",
                "CONTEXT_ACTIVE_TURN_SUMMARY_MAX_CHARS": "300",
            },
            clear=False,
        ):
            context = ContextBuilder().build(
                session=session,
                profile=SimpleNamespace(
                    system_prompt="base",
                    tool_mode="bot",
                ),
                active_turn_start_index=active_turn_start,
            )

        active = context.report.to_dict()["sections"]["active_turn"]
        self.assertTrue(active["truncated"])
        self.assertLess(active["rendered_chars"], active["raw_chars"])
        self.assertIn("active_turn", {item["section"] for item in context.report.to_dict()["reductions"]})

        active_messages = context.messages[-4:]
        self.assertEqual("search and then answer", active_messages[0]["content"])
        self.assertIn("Active turn summary", active_messages[1]["content"])
        self.assertEqual("assistant", active_messages[2]["role"])
        self.assertEqual("call_2", active_messages[2]["tool_calls"][0]["id"])
        self.assertEqual("tool", active_messages[3]["role"])
        self.assertEqual("call_2", active_messages[3]["tool_call_id"])

        rendered_call_ids = [
            (message.get("tool_calls") or [{}])[0].get("id")
            for message in context.messages
            if message.get("tool_calls")
        ]
        self.assertEqual(["call_2"], rendered_call_ids)

    def test_retrieved_history_is_inserted_before_active_turn(self) -> None:
        session = Session(id="web:test")
        session.metadata["user_id"] = "alice"
        session.add_message("user", "解释 trace 字段")

        context = ContextBuilder(
            history_vector_index=FakeVectorIndex(),
            history_scope_resolver=lambda session: "user:alice",
        ).build(
            session=session,
            profile=SimpleNamespace(
                system_prompt="base",
                tool_mode="bot",
            ),
        )

        self.assertIn("<retrieved_history>", context.messages[-2]["content"])
        self.assertIn("tool_result[call_1]", context.messages[-2]["content"])
        self.assertEqual("解释 trace 字段", context.messages[-1]["content"])
        retrieved = context.report.to_dict()["sections"]["retrieved_history"]
        self.assertEqual(1, retrieved["metadata"]["hit_count"])
        self.assertEqual("session_turn", retrieved["metadata"]["hits"][0]["source_type"])
        self.assertEqual(3, retrieved["metadata"]["hits"][0]["message_count"])


if __name__ == "__main__":
    unittest.main()
