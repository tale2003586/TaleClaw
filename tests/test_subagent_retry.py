import time
from types import SimpleNamespace
import unittest

from agents.subagent.parallel import run_parallel_tasks
from runtime.execution.failure_reasons import SubagentFailureReason


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
    def test_unknown_internal_error_is_not_retried(self) -> None:
        runner = FlakyInternalRunner()
        results = run_parallel_tasks(runner=runner, tasks=[{"prompt":"flaky", "agent_type":"explore"}], timeout_seconds=1)
        self.assertEqual(1, runner.calls)
        self.assertFalse(results[0]["success"])
        self.assertEqual(SubagentFailureReason.INTERNAL_ERROR.value, results[0]["failure_reason"])
        self.assertEqual(0, results[0]["retry_count"])

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

    def test_timeout_does_not_start_a_second_worker(self) -> None:
        runner = SlowRunner()

        results = run_parallel_tasks(
            runner=runner,
            tasks=[{"prompt": "slow", "agent_type": "explore"}],
            timeout_seconds=0.001,
        )

        self.assertEqual(1, runner.calls)
        self.assertFalse(results[0]["success"])
        self.assertEqual(SubagentFailureReason.TIMEOUT.value, results[0]["failure_reason"])
        self.assertEqual(0, results[0]["retry_count"])
        self.assertFalse(results[0]["recovered"])


if __name__ == "__main__":
    unittest.main()


def test_auto_retry_requires_explicit_safe_effect_and_termination():
    from agents.subagent.retry import should_auto_retry
    result = {"success": False, "failure_reason": SubagentFailureReason.INTERNAL_ERROR.value,
              "effect_state": "not_started", "worker_terminated": True}
    assert should_auto_retry(result)
    assert not should_auto_retry({**result, "worker_terminated": False})
    assert not should_auto_retry({**result, "effect_state": "unknown"})
    assert not should_auto_retry(result, retry_count=1)
