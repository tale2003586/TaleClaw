import json
from functools import partial
from types import SimpleNamespace
import unittest

from applications.coding.context_state import (
    CODING_CONTEXT_STATE_METADATA_KEY,
    build_coding_context_view,
)
from runtime.context import ContextBuilder
from runtime.context.events import payload_checksum
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
    def _build_context(self, session: Session):
        return ContextBuilder(
            context_providers=DEFAULT_CONTEXT_PROVIDERS,
            coding_context_view_builder=partial(
                build_coding_context_view,
                usable_input_tokens=1_000,
            ),
        ).build(
            session=session,
            profile=SimpleNamespace(system_prompt="base", tool_mode="coding"),
            active_turn_start_index=0,
        )

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
        context = ContextBuilder(
            context_providers=DEFAULT_CONTEXT_PROVIDERS,
            coding_context_view_builder=partial(
                build_coding_context_view,
                usable_input_tokens=1_000,
            ),
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
            "task_state_token_window",
            report["sections"]["active_turn"]["metadata"]["strategy"],
        )

        state = session.metadata[CODING_CONTEXT_STATE_METADATA_KEY]
        self.assertGreaterEqual(state["generation"], 1)
        self.assertGreater(state["prompt_tail_start_index"], 0)
        self.assertNotIn("do_not_repeat", state)

        rendered = "\n".join(str(message.get("content") or "") for message in context.messages)
        self.assertIn("<coding-context-state", rendered)
        self.assertIn("do_not_repeat", rendered)
        self.assertIn("old_a.py", rendered)
        self.assertIn("recent-marker", rendered)
        # A bounded evidence summary may retain the marker, but the 900-line raw
        # tool body must no longer be present in the prompt tail.
        self.assertLess(rendered.count("old-marker-a"), 900)

    def test_repeated_compaction_uses_disjoint_ranges_and_restart_keeps_tail(self) -> None:
        session = Session(id="task:multi-generation", active_agent="coding")
        session.add_message("user", "PINNED-LATEST-REQUEST")
        for index in range(4):
            session.messages.extend(
                _read_file_group(
                    index,
                    path=f"generation_one_{index}.py",
                    marker=f"generation-one-{index}",
                )
            )

        first = self._build_context(session)
        first_state = session.metadata[CODING_CONTEXT_STATE_METADATA_KEY]
        first_ids = set(first_state["last_compaction"]["source_event_ids"])
        first_boundary = session.archive_boundary_seq
        self.assertEqual(
            payload_checksum(session.checkpoints[0]["state"]),
            session.checkpoints[0]["state_sha256"],
        )
        self.assertEqual(
            payload_checksum(session.checkpoints[0]["state"]["task_state"]),
            session.checkpoints[0]["state"]["context_checkpoint"]["checksum"],
        )
        self.assertEqual(1, first_state["generation"])
        self.assertTrue(any(
            message.get("role") == "user"
            and message.get("content") == "PINNED-LATEST-REQUEST"
            for message in first.messages
        ))

        for index in range(4, 8):
            session.messages.extend(
                _read_file_group(
                    index,
                    path=f"generation_two_{index}.py",
                    marker=f"generation-two-{index}",
                )
            )

        second = self._build_context(session)
        second_state = session.metadata[CODING_CONTEXT_STATE_METADATA_KEY]
        second_ids = set(second_state["last_compaction"]["source_event_ids"])
        self.assertEqual(2, second_state["generation"])
        self.assertGreater(session.archive_boundary_seq, first_boundary)
        self.assertTrue(first_ids.isdisjoint(second_ids))
        self.assertTrue(any(
            message.get("role") == "user"
            and message.get("content") == "PINNED-LATEST-REQUEST"
            for message in second.messages
        ))

        restarted = Session(
            id=session.id,
            messages=[dict(message) for message in session.messages],
            active_agent=session.active_agent,
            metadata=json.loads(json.dumps(session.metadata)),
            event_log=[event.to_dict() for event in session.event_log],
            archive_boundary_seq=session.archive_boundary_seq,
            checkpoints=json.loads(json.dumps(session.checkpoints)),
        )
        resumed = self._build_context(restarted)
        resumed_state = restarted.metadata[CODING_CONTEXT_STATE_METADATA_KEY]
        self.assertGreaterEqual(resumed_state["generation"], 2)
        self.assertGreaterEqual(
            resumed_state["prompt_tail_start_index"],
            second_state["prompt_tail_start_index"],
        )
        self.assertTrue(any(
            message.get("role") == "user"
            and message.get("content") == "PINNED-LATEST-REQUEST"
            for message in resumed.messages
        ))

    def test_checkpoint_failure_rolls_back_boundary_and_partial_events(self) -> None:
        session = Session(id="task:checkpoint-failure", active_agent="coding")
        session.add_message("user", "keep the boundary stable")
        for index in range(3):
            session.messages.extend(
                _read_file_group(index, path=f"failure_{index}.py", marker="failure-marker")
            )
        original_boundary = session.archive_boundary_seq

        def fail_after_mutation(*, session, **_kwargs):
            session.metadata["task_state"]["objective"]["summary"] = (
                "corrupted candidate state"
            )
            session.metadata[CODING_CONTEXT_STATE_METADATA_KEY] = {
                "version": 2,
                "generation": 99,
                "compacted_until_event_id": "evt_partial_boundary",
                "prompt_tail_start_index": 999,
                "compacted_until_index": 999,
            }
            session.checkpoints[0]["metadata"]["partial_mutation"] = True
            session.append_event("task_state_checkpoint", {"partial": True})
            session.set_archive_boundary(1)
            raise OSError("checkpoint unavailable")

        context = ContextBuilder(
            context_providers=DEFAULT_CONTEXT_PROVIDERS,
            coding_context_view_builder=partial(
                build_coding_context_view,
                usable_input_tokens=1_000,
                compaction_persister=fail_after_mutation,
            ),
        ).build(
            session=session,
            profile=SimpleNamespace(system_prompt="base", tool_mode="coding"),
            active_turn_start_index=0,
        )

        self.assertEqual(original_boundary, session.archive_boundary_seq)
        self.assertEqual(1, len(session.checkpoints))
        self.assertEqual(
            "legacy_task_state_migration",
            session.checkpoints[0]["metadata"]["migration"]["kind"],
        )
        self.assertNotIn("partial_mutation", session.checkpoints[0]["metadata"])
        self.assertEqual(
            "keep the boundary stable",
            session.metadata["task_state"]["objective"]["summary"],
        )
        self.assertEqual(
            0,
            session.metadata["task_state"]["execution_memory"][
                "compaction_generation"
            ],
        )
        snapshot = session.metadata[CODING_CONTEXT_STATE_METADATA_KEY]
        self.assertEqual(0, snapshot["generation"])
        self.assertEqual("", snapshot["compacted_until_event_id"])
        self.assertEqual(original_boundary, snapshot["compacted_until_index"])
        self.assertFalse(any(
            event.type == "task_state_checkpoint" and event.payload.get("partial")
            for event in session.event_log
        ))
        self.assertFalse(context.report.to_dict()["sections"]["active_turn"]["truncated"])


if __name__ == "__main__":
    unittest.main()
