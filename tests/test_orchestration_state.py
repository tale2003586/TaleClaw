import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agents.subagent.orchestration_state import (
    ORCHESTRATION_STATE_KEY,
    STAGE_TEAMMATE,
    guard_subagent_dispatch,
    record_subagent_dispatch,
    record_subagent_results,
)
from runtime.failure_reasons import SubagentFailureReason


def _session():
    return SimpleNamespace(metadata={})


def _task(description="memory", files=None):
    return {
        "prompt": "locate facts",
        "description": description,
        "scope": {"files": files or ["memory/store.py"]},
        "agent_type": "explore",
    }


def _failure(reason=SubagentFailureReason.STEP_LIMIT.value):
    return {
        "success": False,
        "incomplete": True,
        "failure_reason": reason,
    }


class OrchestrationStateTests(unittest.TestCase):
    def test_same_clue_third_subagent_dispatch_is_rejected(self) -> None:
        session = _session()
        task = _task()

        for _ in range(2):
            decision = guard_subagent_dispatch(session, [task], tool_name="parallel_tasks")
            self.assertTrue(decision.allowed)
            record_subagent_dispatch(session, [task], tool_name="parallel_tasks")
            record_subagent_results(session, [task], [_failure()])

        decision = guard_subagent_dispatch(session, [task], tool_name="parallel_tasks")

        self.assertFalse(decision.allowed)
        self.assertEqual("subagent_orchestration_rejected", decision.reason)
        self.assertIn("spawn_teammate", decision.retry_hint)

    def test_fanout_budget_rejects_after_limit(self) -> None:
        session = _session()
        tasks = [_task(description="clue-a", files=["a.py"])]

        with patch("agents.subagent.orchestration_state.SUBAGENT_MAX_FANOUTS_PER_RUN", 1):
            decision = guard_subagent_dispatch(session, tasks, tool_name="parallel_tasks")
            self.assertTrue(decision.allowed)
            record_subagent_dispatch(session, tasks, tool_name="parallel_tasks")

            rejected = guard_subagent_dispatch(
                session,
                [_task(description="clue-b", files=["b.py"])],
                tool_name="parallel_tasks",
            )

        self.assertFalse(rejected.allowed)
        self.assertEqual("subagent_fanout_budget_exceeded", rejected.reason)

    def test_stage_cannot_move_back_to_subagent(self) -> None:
        session = _session()
        task = _task()
        state = session.metadata.setdefault(
            ORCHESTRATION_STATE_KEY,
            {"fanout_count": 0, "fanout_rejected_count": 0, "clues": {}},
        )
        clue_key = guard_subagent_dispatch(session, [task], tool_name="task")
        self.assertTrue(clue_key.allowed)
        record_subagent_dispatch(session, [task], tool_name="task")
        key = next(iter(state["clues"]))
        state["clues"][key]["stage"] = STAGE_TEAMMATE

        rejected = guard_subagent_dispatch(session, [task], tool_name="task")

        self.assertFalse(rejected.allowed)
        self.assertIn("degradation ladder", rejected.message)
        self.assertEqual(1, session.metadata[ORCHESTRATION_STATE_KEY]["fanout_rejected_count"])

    def test_rejection_payload_is_json_serializable(self) -> None:
        session = _session()
        task = _task()
        for _ in range(2):
            record_subagent_dispatch(session, [task], tool_name="task")
            record_subagent_results(session, [task], [_failure()])

        decision = guard_subagent_dispatch(session, [task], tool_name="task")
        payload = decision.to_payload()

        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertIn("subagent_orchestration_rejected", encoded)


if __name__ == "__main__":
    unittest.main()
