from __future__ import annotations

import unittest

from applications.coding.compaction import (
    DeterministicEventExtractor,
    StatePatch,
    StateValidationError,
    reduce_task_state,
    validate_state_patch,
)
from applications.coding.task_state import (
    Action,
    EvidenceRef,
    Finding,
    Hypothesis,
    ItemStatus,
    Objective,
    PlanItem,
    TaskPhase,
    TaskState,
    load_task_state,
    migrate_coding_context_state_payload,
    migrate_working_memory_payload,
)
from runtime.sessions.session import Session
from tools.handlers import TASK_HANDLERS


class TaskStateCoreTests(unittest.TestCase):
    def test_load_prefers_newer_metadata_but_recovers_from_checkpoint(self) -> None:
        checkpoint_state = TaskState(objective=Objective("checkpoint"), version=2)
        live_state = TaskState(objective=Objective("live metadata"), version=3)
        session = Session(
            id="task:state-recovery",
            metadata={"task_state": live_state.to_dict()},
            checkpoints=[{
                "state": {"task_state": checkpoint_state.to_dict()},
            }],
        )

        self.assertEqual("live metadata", load_task_state(session).objective.summary)

        session.metadata["task_state"] = {"invalid": True}
        self.assertEqual("checkpoint", load_task_state(session).objective.summary)

    def test_task_state_round_trip_keeps_only_objective_reference(self) -> None:
        state = TaskState(
            objective=Objective("Refactor context", "event://evt_request"),
            phase=TaskPhase.PLANNING,
            evidence_index={"ev:1": EvidenceRef("ev:1", "evt:1", summary="read file")},
        )

        restored = TaskState.from_payload(state.to_dict())

        self.assertIsNotNone(restored)
        self.assertEqual("event://evt_request", restored.objective.original_request_ref)
        self.assertEqual(TaskPhase.PLANNING, restored.phase)
        self.assertEqual("evt:1", restored.evidence_index["ev:1"].event_id)

    def test_legacy_migrations_are_idempotent_and_downgrade_unsupported_finding(self) -> None:
        working = {
            "objective": "x" * 900,
            "completed_units": [{"unit_id": "done", "conclusion": "completed", "evidence_refs": ["ev:1"]}],
            "pending_units": [{"unit_id": "next", "description": "continue", "state": "todo"}],
            "archived_findings": {"unverified": "legacy observation"},
        }
        first = migrate_working_memory_payload(working, original_request_ref="evt:req")
        second = migrate_working_memory_payload(working, original_request_ref="evt:req")
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertLessEqual(len(first.objective.summary), 480)
        self.assertEqual("evt:req", first.objective.original_request_ref)
        self.assertEqual(ItemStatus.PENDING, first.pending_actions[0].status)
        self.assertEqual(1, len(first.hypotheses))

        legacy_context = {
            "objective": "inspect context",
            "evidence_index": {"ev:1": {"event_id": "evt:1", "summary": "observed"}},
            "findings": [
                {"id": "verified", "claim": "supported", "evidence_refs": ["ev:1"]},
                {"id": "unsupported", "claim": "not supported"},
            ],
            "do_not_repeat": [{"tool": "read_file"}],
        }
        migrated = migrate_coding_context_state_payload(legacy_context)
        self.assertEqual(["verified"], [item.id for item in migrated.findings])
        self.assertEqual(["unsupported"], [item.id for item in migrated.hypotheses])
        self.assertEqual("read_file", migrated.execution_memory.do_not_repeat[0]["tool"])

    def test_extractor_emits_evidence_and_coverage_without_inventing_findings(self) -> None:
        patch = DeterministicEventExtractor().extract([
            {
                "event_id": "evt:tool", "event_type": "tool_result",
                "payload": {"path": "applications/coding/task_state.py", "summary": "class TaskState"},
            },
            {"event_id": "evt:text", "event_type": "assistant_message", "payload": {"content": "unstructured claim"}},
        ])

        self.assertEqual(["evidence:evt:tool"], [item.id for item in patch.evidence])
        self.assertEqual("applications/coding/task_state.py", patch.coverage[0].area)
        self.assertEqual([], patch.findings)

    def test_runtime_error_is_reduced_into_a_blocker(self) -> None:
        patch = DeterministicEventExtractor().extract([
            {
                "event_id": "evt:error",
                "event_type": "runtime_error",
                "payload": {"error": "database unavailable"},
            }
        ])

        state = reduce_task_state(TaskState(objective=Objective("recover")), patch)

        self.assertEqual(["blocker:evt:error"], [item.id for item in state.blockers])
        self.assertIn("database unavailable", state.blockers[0].description)

    def test_validator_rejects_invented_evidence_and_illegal_transitions(self) -> None:
        state = TaskState(objective=Objective("test"), phase=TaskPhase.PLANNING)
        invented = StatePatch(findings=[Finding("finding:bad", "claim", ["ev:missing"])])
        with self.assertRaisesRegex(StateValidationError, "unknown evidence"):
            validate_state_patch(state, invented)

        illegal = StatePatch(phase=TaskPhase.VERIFICATION)
        with self.assertRaisesRegex(StateValidationError, "illegal phase"):
            validate_state_patch(state, illegal)

    def test_validator_does_not_reject_large_but_structurally_valid_state(self) -> None:
        state = TaskState(objective=Objective("x" * 40_000))
        patch = StatePatch(
            hypotheses=[
                Hypothesis("hypothesis:large", "still needs verification")
            ]
        )

        updated = reduce_task_state(state, patch, max_tokens=1)

        self.assertEqual("hypothesis:large", updated.hypotheses[0].id)

    def test_update_task_state_tool_normalizes_patch_and_allows_retry(self) -> None:
        session = Session(id="task:state-tool", active_agent="coding")
        session.add_message("user", "inspect state updates")

        with self.assertRaisesRegex(StateValidationError, "unknown evidence"):
            TASK_HANDLERS["update_task_state"](
                _session=session,
                add_findings=[{
                    "claim": "unsupported",
                    "evidence_refs": ["evidence:missing"],
                }],
            )

        TASK_HANDLERS["update_task_state"](
            _session=session,
            add_evidence=[{
                "id": "evidence:evt:read",
                "event_id": "evt:read",
                "summary": "read target.py",
                "path": "target.py",
            }],
            add_findings=[{
                "claim": "target.py was inspected",
                "evidence_refs": ["evidence:evt:read"],
            }],
        )

        state = load_task_state(session)
        self.assertIn("evidence:evt:read", state.evidence_index)
        self.assertEqual("target.py was inspected", state.findings[0].claim)

    def test_reducer_records_replacement_history_and_checks_action_transitions(self) -> None:
        state = TaskState(
            objective=Objective("test"),
            pending_actions=[Action("action:1", "read", ItemStatus.PENDING)],
        )
        revised = Action("action:1", "read again", ItemStatus.PENDING, supersedes="action:1")
        updated = reduce_task_state(state, StatePatch(pending_actions=[revised]))
        self.assertEqual("read again", updated.pending_actions[0].description)
        self.assertEqual(1, len(updated.history))

        terminal = TaskState(
            objective=Objective("test"),
            pending_actions=[Action("action:1", "done", ItemStatus.COMPLETED)],
        )
        invalid = StatePatch(action_transitions=[])
        invalid.action_transitions.append(
            __import__("applications.coding.compaction", fromlist=["ItemTransition"]).ItemTransition(
                "action:1", ItemStatus.IN_PROGRESS
            )
        )
        with self.assertRaisesRegex(StateValidationError, "illegal action"):
            validate_state_patch(terminal, invalid)


if __name__ == "__main__":
    unittest.main()
