import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from applications.coding.context_state import (
    CODING_CONTEXT_STATE_METADATA_KEY,
    build_coding_context_view,
)
from runtime.context import ContextBuilder
from runtime.context.providers import DEFAULT_CONTEXT_PROVIDERS
from runtime.sessions.session import Session


def _read_file_group(index: int, *, path: str, marker: str) -> list[dict]:
    call_id = f"call_{index}"
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": path}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "read_file",
            "status": "success",
            "content": (
                f"class Symbol{index}:\n"
                f"    pass\n"
                + (f"{marker}\n" * 900)
            ),
        },
    ]


class CodingContextStateTests(unittest.TestCase):
    def test_coding_context_state_compacts_old_tool_groups(self) -> None:
        session = Session(id="task:coding-state", active_agent="coding")
        session.add_message("user", "请检查这些文件并总结下一步")
        session.messages.extend(
            _read_file_group(0, path="old_a.py", marker="old-marker-a")
        )
        session.messages.extend(
            _read_file_group(1, path="old_b.py", marker="old-marker-b")
        )
        session.messages.extend(
            _read_file_group(2, path="recent.py", marker="recent-marker")
        )

        profile = SimpleNamespace(system_prompt="base", tool_mode="coding")
        with (
            patch("runtime.context.builder.CODING_CONTEXT_STATE_ENABLED", True),
            patch("runtime.context.builder.CODING_CONTEXT_COMPACTION_TRIGGER_TOKENS", 1000),
            patch("runtime.context.builder.CODING_CONTEXT_COMPACTION_TARGET_TOKENS", 500),
            patch("runtime.context.builder.CODING_CONTEXT_RECENT_GROUPS", 1),
        ):
            context = ContextBuilder(
                context_providers=DEFAULT_CONTEXT_PROVIDERS,
                coding_context_view_builder=build_coding_context_view,
            ).build(
                session=session,
                profile=profile,
                active_turn_start_index=0,
            )

        report = context.report.to_dict()
        self.assertTrue(report["metadata"]["coding_context_state_enabled"])
        self.assertIn("coding_context_state", report["sections"])
        self.assertTrue(report["sections"]["active_turn"]["truncated"])
        self.assertEqual(
            "coding_context_state",
            report["sections"]["active_turn"]["metadata"]["strategy"],
        )

        state = session.metadata[CODING_CONTEXT_STATE_METADATA_KEY]
        self.assertGreaterEqual(state["generation"], 1)
        self.assertGreater(state["prompt_tail_start_index"], 0)
        self.assertTrue(state["do_not_repeat"])

        rendered = "\n".join(str(message.get("content") or "") for message in context.messages)
        self.assertIn("<coding-context-state", rendered)
        self.assertIn("do_not_repeat", rendered)
        self.assertIn("old_a.py", rendered)
        self.assertIn("recent-marker", rendered)
        self.assertNotIn("old-marker-a", rendered)


if __name__ == "__main__":
    unittest.main()
