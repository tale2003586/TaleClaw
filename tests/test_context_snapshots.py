from types import SimpleNamespace
import unittest

from runtime.context.snapshots import (
    CompactionLimits,
    CompactionOutput,
    ContextSnapshotManager,
    EventCompactor,
    SnapshotStatus,
    active_context_snapshot,
)
from runtime.sessions import Session
from runtime.sessions.session_store import _restored_archive_boundary


VALID_SUMMARY = """Global objective: close runtime architecture
Constraints:
- preserve state boundaries
Pending work:
- finish verification
"""


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(content=response)


def task_state(version=3):
    return SimpleNamespace(
        version=version,
        objective=SimpleNamespace(summary="close runtime architecture"),
        constraints=[SimpleNamespace(description="preserve state boundaries")],
        pending_actions=[SimpleNamespace(description="finish verification")],
        findings=[],
        decisions=[],
        blockers=[],
    )


def events():
    return [
        {"seq": 1, "event_id": "evt-1", "payload": {"message": "start"}},
        {"seq": 2, "event_id": "evt-2", "payload": {"message": "result"}},
    ]


def output(strategy="normal"):
    return CompactionOutput(
        summary=VALID_SUMMARY,
        strategy=strategy,
        attempt_log=({"strategy": strategy, "status": "success"},),
    )


def prepare(manager, session, *, state_version=3):
    return manager.prepare(
        session,
        output=output(),
        events=events(),
        source_task_state_version=state_version,
        evidence_refs=[],
        artifact_refs=[],
    )


class ContextSnapshotTests(unittest.TestCase):
    def compact(self, responses, **limit_overrides):
        provider = ScriptedProvider(responses)
        limits = CompactionLimits(**limit_overrides)
        result = EventCompactor(provider=provider, model="summary", limits=limits).compact(
            events=events(),
            task_state=task_state(),
            covered_start_seq=1,
            covered_end_seq=2,
        )
        return result, provider

    def test_normal_success(self):
        result, provider = self.compact([VALID_SUMMARY])
        self.assertEqual("normal", result.strategy)
        self.assertEqual([], provider.calls[0]["tools"])

    def test_validation_failure_uses_repair_with_specific_error(self):
        result, provider = self.compact(["too vague", VALID_SUMMARY])
        self.assertEqual("repair", result.strategy)
        self.assertIn("global objective is missing", provider.calls[1]["messages"][0]["content"])

    def test_repair_failure_uses_chunked_path(self):
        result, _ = self.compact(["bad", "still bad", VALID_SUMMARY])
        self.assertEqual("chunked", result.strategy)

    def test_provider_failures_use_deterministic_fallback(self):
        result, provider = self.compact([
            RuntimeError("normal failed"),
            RuntimeError("repair failed"),
            RuntimeError("chunk failed"),
        ])
        self.assertEqual("deterministic", result.strategy)
        self.assertEqual(3, len(provider.calls))
        self.assertIn("close runtime architecture", result.summary)

    def test_prepared_is_invisible_until_activation(self):
        session = Session(id="snapshot:prepared")
        prepared = prepare(ContextSnapshotManager(), session)
        self.assertEqual(SnapshotStatus.PREPARED, prepared.status)
        self.assertIsNone(active_context_snapshot(session))
        self.assertEqual(0, session.archive_boundary_seq)

        active = ContextSnapshotManager().activate(session, prepared.snapshot_id)
        self.assertEqual(SnapshotStatus.ACTIVE, active.status)
        self.assertEqual(active.snapshot_id, active_context_snapshot(session).snapshot_id)
        self.assertEqual(2, session.archive_boundary_seq)

    def test_activation_failure_does_not_archive(self):
        session = Session(id="snapshot:activate-failure")
        prepared = prepare(ContextSnapshotManager(), session)
        manager = ContextSnapshotManager(
            activation_writer=lambda *_: (_ for _ in ()).throw(RuntimeError("db down"))
        )
        with self.assertRaises(RuntimeError):
            manager.activate(session, prepared.snapshot_id)
        self.assertIsNone(active_context_snapshot(session))
        self.assertEqual(0, session.archive_boundary_seq)

    def test_archive_failure_keeps_active_and_recovers_without_regeneration(self):
        calls = []

        def archive_writer(*_):
            calls.append("archive")
            if len(calls) == 1:
                raise RuntimeError("archive unavailable")

        session = Session(id="snapshot:archive-failure")
        prepared = prepare(ContextSnapshotManager(), session)
        manager = ContextSnapshotManager(archive_writer=archive_writer)
        with self.assertRaises(RuntimeError):
            manager.activate(session, prepared.snapshot_id)
        self.assertEqual(prepared.snapshot_id, active_context_snapshot(session).snapshot_id)

        recovered = manager.recover(session)
        self.assertTrue(recovered.archive_completed)
        self.assertEqual(["archive", "archive"], calls)

    def test_restart_recovers_prepared_snapshot(self):
        session = Session(id="snapshot:restart")
        prepared = prepare(ContextSnapshotManager(), session)
        restarted = Session(
            id=session.id,
            context_snapshots=list(session.context_snapshots),
            event_log=list(session.event_log),
        )
        recovered = ContextSnapshotManager().recover(restarted)
        self.assertEqual(prepared.snapshot_id, recovered.snapshot_id)
        self.assertEqual(SnapshotStatus.ACTIVE, recovered.status)

    def test_same_source_range_is_idempotent(self):
        session = Session(id="snapshot:idempotent")
        manager = ContextSnapshotManager()
        first = prepare(manager, session)
        second = prepare(manager, session)
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(1, len(session.context_snapshots))

    def test_restart_boundary_comes_from_archived_active_snapshot(self):
        snapshots = [{
            "status": "active",
            "archive_completed": True,
            "covered_event_end_seq": 9,
        }, {
            "status": "prepared",
            "archive_completed": False,
            "covered_event_end_seq": 20,
        }]
        self.assertEqual(9, _restored_archive_boundary([], snapshots))
        self.assertEqual(
            12,
            _restored_archive_boundary(
                [{"archive_boundary_seq": 12}],
                snapshots,
            ),
        )

    def test_snapshot_and_task_state_failures_are_independent(self):
        state = task_state(version=7)
        session = Session(id="snapshot:state-independent")
        manager = ContextSnapshotManager(
            prepare_writer=lambda *_: (_ for _ in ()).throw(RuntimeError("snapshot write failed"))
        )
        with self.assertRaises(RuntimeError):
            prepare(manager, session, state_version=state.version)
        self.assertEqual(7, state.version)
        self.assertEqual([], session.context_snapshots)

        committed = prepare(ContextSnapshotManager(), session, state_version=state.version)
        state_update_error = RuntimeError("TaskState update failed")
        self.assertIsInstance(state_update_error, RuntimeError)
        self.assertEqual(7, committed.source_task_state_version)


if __name__ == "__main__":
    unittest.main()
