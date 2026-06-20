import time
from types import SimpleNamespace
import unittest

from agents.subagent.parallel import run_parallel_tasks
from runtime.failure_reasons import SubagentFailureReason


class FlakyInternalRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient provider failure")
        return SimpleNamespace(
            to_dict=lambda: {
                "agent_type": kwargs["agent_type"],
                "success": True,
                "summary": "recovered",
                "status": "completed",
                "files_touched": [],
                "tool_count": 0,
                "error": None,
                "incomplete": False,
            }
        )


class StepLimitRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            to_dict=lambda: {
                "agent_type": kwargs["agent_type"],
                "success": False,
                "summary": "",
                "status": "failed",
                "files_touched": [],
                "tool_count": 0,
                "error": "step limit",
                "truncated": True,
                "stop_reason": "reasoning_step_limit",
                "findings": [],
                "incomplete": True,
                "failure_reason": SubagentFailureReason.STEP_LIMIT.value,
                "failure_message": "hit step limit",
                "recoverable": True,
                "retry_hint": "split narrower",
                "evidence": [],
            }
        )


class SlowRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, **kwargs):
        self.calls += 1
        time.sleep(0.03)
        return SimpleNamespace(
            to_dict=lambda: {
                "agent_type": kwargs["agent_type"],
                "success": True,
                "summary": "too late",
                "status": "completed",
            }
        )


class SubagentRetryTests(unittest.TestCase):
    def test_internal_error_is_retried_once_and_can_recover(self) -> None:
        runner = FlakyInternalRunner()

        results = run_parallel_tasks(
            runner=runner,
            tasks=[{"prompt": "flaky", "agent_type": "explore"}],
            timeout_seconds=1,
        )

        self.assertEqual(2, runner.calls)
        self.assertTrue(results[0]["success"])
        self.assertEqual("recovered", results[0]["summary"])
        self.assertEqual(1, results[0]["retry_count"])
        self.assertTrue(results[0]["recovered"])
        self.assertEqual(
            SubagentFailureReason.INTERNAL_ERROR.value,
            results[0]["recovered_from_failure_reason"],
        )

    def test_step_limit_is_not_auto_retried(self) -> None:
        runner = StepLimitRunner()

        results = run_parallel_tasks(
            runner=runner,
            tasks=[{"prompt": "too broad", "agent_type": "explore"}],
            timeout_seconds=1,
        )

        self.assertEqual(1, runner.calls)
        self.assertFalse(results[0]["success"])
        self.assertEqual(SubagentFailureReason.STEP_LIMIT.value, results[0]["failure_reason"])
        self.assertNotIn("retry_count", results[0])

    def test_timeout_is_retried_once_and_then_reported(self) -> None:
        runner = SlowRunner()

        results = run_parallel_tasks(
            runner=runner,
            tasks=[{"prompt": "slow", "agent_type": "explore"}],
            timeout_seconds=0.001,
        )

        self.assertEqual(2, runner.calls)
        self.assertFalse(results[0]["success"])
        self.assertEqual(SubagentFailureReason.TIMEOUT.value, results[0]["failure_reason"])
        self.assertEqual(1, results[0]["retry_count"])
        self.assertFalse(results[0]["recovered"])


if __name__ == "__main__":
    unittest.main()
