from __future__ import annotations

import unittest

from applications.coding.compaction import (
    CompactionCoordinator,
    CompactionError,
    SemanticCompactor,
    StatePatch,
)
from applications.coding.task_state import Finding, Objective, TaskState
from runtime.context.dynamic_budget import PromptBudgetExceeded


class TaskStateCompactionTests(unittest.TestCase):
    def test_semantic_chat_is_hard_guarded_before_provider_call(self) -> None:
        class TinyProvider:
            context_limit = 200

            def __init__(self) -> None:
                self.calls = 0

            def count_tokens(self, messages):
                return sum(len(str(item.get("content") or "")) for item in messages)

            def chat(self, **_kwargs):
                self.calls += 1
                raise AssertionError("provider must not be called")

        provider = TinyProvider()
        compactor = SemanticCompactor(provider=provider, model="tiny", max_tokens=256)

        with self.assertRaises(PromptBudgetExceeded):
            compactor.propose(
                state=TaskState(objective=Objective("x" * 1_000)),
                events=[{
                    "event_id": "evt:large",
                    "event_type": "user_message",
                    "payload": {"content": "y" * 1_000},
                }],
            )

        self.assertEqual(0, provider.calls)

    def test_closed_tool_transaction_is_checkpointed_but_open_one_is_not_archived(self) -> None:
        persisted = []
        coordinator = CompactionCoordinator(
            checkpoint_writer=lambda checkpoint: persisted.append(("checkpoint", checkpoint)),
            completion_writer=lambda event: persisted.append(("completion", event)),
            archive_callback=lambda ids: persisted.append(("archive", ids)),
        )
        events = [
            {"event_id": "evt:user", "event_type": "user_message", "payload": {}},
            {"event_id": "evt:call-1", "event_type": "tool_call", "payload": {"call_id": "call-1"}},
            {"event_id": "evt:result-1", "event_type": "tool_result", "payload": {"call_id": "call-1", "path": "a.py"}},
            {"event_id": "evt:call-2", "event_type": "tool_call", "payload": {"call_id": "call-2"}},
        ]

        result = coordinator.compact(state=TaskState(objective=Objective("test")), events=events)

        self.assertEqual("evt:result-1", result.compacted_until_event_id)
        self.assertEqual(["evt:user", "evt:call-1", "evt:result-1"], result.archived_event_ids)
        self.assertEqual(["checkpoint", "completion", "archive"], [entry[0] for entry in persisted])
        self.assertEqual("evt:result-1", persisted[-1][1][-1])

    def test_callback_failure_does_not_advance_state_or_boundary(self) -> None:
        original = TaskState(objective=Objective("test"))
        called = []
        coordinator = CompactionCoordinator(
            checkpoint_writer=lambda checkpoint: called.append("checkpoint"),
            completion_writer=lambda event: (_ for _ in ()).throw(RuntimeError("db unavailable")),
            archive_callback=lambda ids: called.append("archive"),
        )

        with self.assertRaises(CompactionError):
            coordinator.compact(
                state=original,
                events=[{"event_id": "evt:1", "event_type": "user_message", "payload": {}}],
                compacted_until_event_id=None,
            )

        self.assertEqual(0, original.execution_memory.compaction_generation)
        self.assertEqual(1, original.version)
        self.assertEqual(["checkpoint"], called)

    def test_semantic_failure_retries_once_without_persisting_candidate(self) -> None:
        original = TaskState(objective=Objective("keep this objective"))
        attempts = []
        persisted = []

        def fail_semantic(*, state, events):
            attempts.append((state.version, len(events)))
            raise RuntimeError("semantic model unavailable")

        coordinator = CompactionCoordinator(
            checkpoint_writer=lambda checkpoint: persisted.append("checkpoint"),
            completion_writer=lambda event: persisted.append("completion"),
            archive_callback=lambda ids: persisted.append("archive"),
            semantic_compactor=SemanticCompactor(fail_semantic),
        )

        with self.assertRaisesRegex(CompactionError, "failed after one retry"):
            coordinator.compact(
                state=original,
                events=[{
                    "event_id": "evt:1",
                    "event_type": "user_message",
                    "payload": {},
                }],
            )

        self.assertEqual(2, len(attempts))
        self.assertEqual([], persisted)
        self.assertEqual(1, original.version)
        self.assertEqual(0, original.execution_memory.compaction_generation)

    def test_invalid_semantic_patch_does_not_persist_or_advance_state(self) -> None:
        original = TaskState(objective=Objective("authoritative objective"))
        persisted = []
        coordinator = CompactionCoordinator(
            checkpoint_writer=lambda checkpoint: persisted.append("checkpoint"),
            completion_writer=lambda event: persisted.append("completion"),
            archive_callback=lambda ids: persisted.append("archive"),
            semantic_compactor=SemanticCompactor(
                lambda *, state, events: StatePatch(
                    objective=Objective("invented replacement")
                )
            ),
        )

        with self.assertRaisesRegex(CompactionError, "failed after one retry"):
            coordinator.compact(
                state=original,
                events=[{
                    "event_id": "evt:1",
                    "event_type": "user_message",
                    "payload": {},
                }],
            )

        self.assertEqual([], persisted)
        self.assertEqual("authoritative objective", original.objective.summary)
        self.assertEqual(1, original.version)

    def test_semantic_patch_can_add_supported_finding_but_cannot_rewrite_objective(self) -> None:
        compactor = SemanticCompactor(lambda *, state, events: StatePatch(
            findings=[Finding("finding:1", "file observed", ["evidence:evt:tool"])],
        ))
        coordinator = CompactionCoordinator(
            checkpoint_writer=lambda checkpoint: None,
            completion_writer=lambda event: None,
            archive_callback=lambda ids: None,
            semantic_compactor=compactor,
        )
        result = coordinator.compact(
            state=TaskState(objective=Objective("keep this objective")),
            events=[{"event_id": "evt:tool", "event_type": "tool_result", "payload": {"summary": "read"}}],
        )

        self.assertEqual("keep this objective", result.state.objective.summary)
        self.assertEqual("finding:1", result.state.findings[0].id)

    def test_reducer_assigns_stable_ids_to_new_semantic_items(self) -> None:
        events = [{
            "event_id": "evt:user",
            "event_type": "user_message",
            "payload": {},
        }]

        def compact_once():
            coordinator = CompactionCoordinator(
                checkpoint_writer=lambda checkpoint: None,
                completion_writer=lambda event: None,
                archive_callback=lambda ids: None,
                semantic_compactor=SemanticCompactor(
                    lambda *, state, events: {
                        "add_hypotheses": [{"claim": "the cause still needs verification"}],
                        "add_pending_actions": [{"description": "verify the cause"}],
                    }
                ),
            )
            return coordinator.compact(
                state=TaskState(objective=Objective("diagnose")),
                events=events,
            ).state

        first = compact_once()
        second = compact_once()

        self.assertTrue(first.hypotheses[0].id.startswith("hypothesis:"))
        self.assertTrue(first.pending_actions[0].id.startswith("action:"))
        self.assertEqual(first.hypotheses[0].id, second.hypotheses[0].id)
        self.assertEqual(first.pending_actions[0].id, second.pending_actions[0].id)


if __name__ == "__main__":
    unittest.main()
