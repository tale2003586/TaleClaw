import unittest
from unittest.mock import patch

from runtime.context_budget import ContextBudgeter, SectionBudgetRule
from runtime.context_history import budget_active_turn


def _assistant_tool_call(call_id: str, name: str) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }],
    }


class ContextBudgetTests(unittest.TestCase):
    def test_budgeter_defaults_enabled_and_tuned(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "CONTEXT_ENABLE_SECTION_BUDGET": "",
                "CONTEXT_MEMORY_BUDGET": "",
                "CONTEXT_RETRIEVED_HISTORY_BUDGET": "",
                "CONTEXT_SECURITY_KNOWLEDGE_BUDGET": "",
                "CONTEXT_TASK_RUNTIME_EVENTS_BUDGET": "",
                "CONTEXT_CONVERSATION_HISTORY_BUDGET": "",
                "CONTEXT_ACTIVE_TURN_PRESERVE_TOOLS": "",
            },
            clear=False,
        ):
            budgeter = ContextBudgeter.from_env()

        self.assertTrue(budgeter.enabled)
        self.assertEqual(2000, budgeter.rules["memory"].budget_chars)
        self.assertEqual(2500, budgeter.rules["retrieved_history"].budget_chars)
        self.assertEqual(3000, budgeter.rules["security_knowledge"].budget_chars)
        self.assertEqual(1500, budgeter.rules["task_runtime_events"].budget_chars)
        self.assertEqual(16000, budgeter.rules["conversation_history"].budget_chars)
        self.assertIn("code_outline", budgeter.rules["active_turn"].preserve_tools)
        self.assertIn("repo_map", budgeter.rules["active_turn"].preserve_tools)

    def test_active_turn_compresses_old_unpreserved_tool_results(self) -> None:
        rule = ContextBudgeter.from_env().rules["active_turn"]
        messages = [
            {"role": "user", "content": "current request"},
            _assistant_tool_call("call-1", "web_search"),
            {"role": "tool", "tool_call_id": "call-1", "content": "old search result" * 100},
            _assistant_tool_call("call-2", "read_file"),
            {"role": "tool", "tool_call_id": "call-2", "content": "important file result" * 100},
            _assistant_tool_call("call-3", "web_search"),
            {"role": "tool", "tool_call_id": "call-3", "content": "recent search result" * 100},
        ]

        with patch.dict(
            "os.environ",
            {
                "CONTEXT_ACTIVE_TURN_KEEP_RECENT_RESULTS": "1",
                "CONTEXT_ACTIVE_TURN_PRESERVE_TOOLS": "read_file",
            },
            clear=False,
        ):
            rule = ContextBudgeter.from_env().rules["active_turn"]
            budgeted = budget_active_turn(messages, enabled=True, rule=rule)

        contents = [message.get("content") for message in budgeted.rendered_messages]
        self.assertIn("<web_search result compressed for context budget>", contents)
        self.assertTrue(any("important file result" in str(item) for item in contents))
        self.assertTrue(any("recent search result" in str(item) for item in contents))
        self.assertEqual(1, budgeted.metadata["compressed_tool_results"])

    def test_active_turn_preserves_latest_read_map_and_outline_results(self) -> None:
        rule = SectionBudgetRule(
            name="active_turn",
            budget_chars=20000,
            floor_chars=3000,
            strategy="latest_tool_call",
            keep_recent_results=0,
            preserve_tools=(),
        )
        messages = [
            {"role": "user", "content": "current request"},
            _assistant_tool_call("call-old-read", "read_file"),
            {
                "role": "tool",
                "tool_call_id": "call-old-read",
                "content": "old read result " * 100,
                "final_arguments": {"path": "old.py", "offset": 20, "limit": 20},
            },
            _assistant_tool_call("call-old-outline", "code_outline"),
            {
                "role": "tool",
                "tool_call_id": "call-old-outline",
                "content": "old outline result " * 100,
                "final_arguments": {"path": "old_large.py", "offset": 10, "limit": 10},
            },
            _assistant_tool_call("call-map", "repo_map"),
            {
                "role": "tool",
                "tool_call_id": "call-map",
                "content": "repo map result " * 100,
                "final_arguments": {"path": ".", "offset": 0, "max_depth": 2},
            },
            _assistant_tool_call("call-new-read", "read_file"),
            {
                "role": "tool",
                "tool_call_id": "call-new-read",
                "content": "latest read result " * 100,
                "final_arguments": {"path": "new.py", "offset": 0},
            },
            _assistant_tool_call("call-outline", "code_outline"),
            {
                "role": "tool",
                "tool_call_id": "call-outline",
                "content": "outline result " * 100,
                "final_arguments": {"path": "large.py", "offset": 0},
            },
        ]

        budgeted = budget_active_turn(messages, enabled=True, rule=rule)

        text = "\n".join(str(message.get("content", "")) for message in budgeted.rendered_messages)
        self.assertIn("read_file result compressed", text)
        self.assertIn("path=old.py; offset=20, limit=20", text)
        self.assertIn("code_outline result compressed", text)
        self.assertIn("re-outline with code_outline(path=\"old_large.py\", offset=10, limit=10)", text)
        self.assertIn("repo map result", text)
        self.assertIn("latest read result", text)
        self.assertIn("outline result", text)


if __name__ == "__main__":
    unittest.main()
