import time
from types import SimpleNamespace
import unittest

from agents.subagent.parallel import MAX_PARALLEL_SUBTASKS, run_parallel_tasks
from runtime.failure_reasons import SubagentFailureReason


class FakeRunner:
    def __init__(self, *, delay=0.0, fail_on=None) -> None:
        self.delay = delay
        self.fail_on = fail_on or set()
        self.calls = []

    def run(self, *, prompt, agent_type, description="", parent_session=None):
        self.calls.append((prompt, agent_type, description, parent_session))
        if self.delay:
            time.sleep(self.delay)
        if prompt in self.fail_on:
            raise RuntimeError(f"failed {prompt}")
        return SimpleNamespace(
            to_dict=lambda: {
                "agent_type": agent_type,
                "success": True,
                "summary": f"done: {prompt}",
                "files_touched": [],
                "tool_count": 0,
                "error": None,
            }
        )


class ParallelTasksTests(unittest.TestCase):
    def test_runs_tasks_concurrently_and_preserves_order(self) -> None:
        runner = FakeRunner(delay=0.02)
        tasks = [
            {"agent_type": "explore", "prompt": "a"},
            {"agent_type": "plan", "prompt": "b"},
        ]
        started = time.perf_counter()

        results = run_parallel_tasks(runner=runner, tasks=tasks, max_workers=2)

        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.04)
        self.assertEqual(["done: a", "done: b"], [item["summary"] for item in results])
        self.assertEqual(2, len(runner.calls))

    def test_caps_task_count_at_eight_and_reports_exceptions(self) -> None:
        runner = FakeRunner(fail_on={"bad"})
        tasks = [{"prompt": str(i)} for i in range(MAX_PARALLEL_SUBTASKS + 2)]
        tasks[3]["prompt"] = "bad"

        results = run_parallel_tasks(runner=runner, tasks=tasks, max_workers=20)

        self.assertEqual(MAX_PARALLEL_SUBTASKS, len(results))
        self.assertEqual(MAX_PARALLEL_SUBTASKS + 1, len(runner.calls))
        self.assertFalse(results[3]["success"])
        self.assertIn("RuntimeError", results[3]["error"])
        self.assertEqual(SubagentFailureReason.INTERNAL_ERROR.value, results[3]["failure_reason"])
        self.assertEqual("failed", results[3]["status"])
        self.assertEqual(1, results[3]["retry_count"])

    def test_timeout_returns_structured_error_without_waiting_forever(self) -> None:
        runner = FakeRunner(delay=0.03)
        started = time.perf_counter()

        results = run_parallel_tasks(
            runner=runner,
            tasks=[{"prompt": "slow", "agent_type": "explore"}],
            timeout_seconds=0.001,
        )

        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.02)
        self.assertFalse(results[0]["success"])
        self.assertIn("TimeoutError", results[0]["error"])
        self.assertEqual(SubagentFailureReason.TIMEOUT.value, results[0]["failure_reason"])
        self.assertEqual("timeout", results[0]["stop_reason"])
        self.assertTrue(results[0]["recoverable"])


if __name__ == "__main__":
    unittest.main()
