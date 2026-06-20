import json
import tempfile
import unittest
from pathlib import Path

from runtime.trace.failure import FailureCategory, classify_failure
from runtime.trace.run_state import RunState
from runtime.trace.trace_store import TraceStore


class TraceFailureSummaryTests(unittest.TestCase):
    def test_classifies_workspace_violation_from_hook_trace(self) -> None:
        classification = classify_failure(
            run_state={"status": "stopped"},
            events=[
                {
                    "event": "tool.call.completed",
                    "payload": {
                        "tool_name": "bash",
                        "status": "denied",
                        "output_preview": "Error: Shell command changes directory outside workspace.",
                        "pre_hook_trace": [
                            {
                                "hook_name": "shell_workspace_scope",
                                "decision": "deny",
                                "reason": "outside workspace",
                            }
                        ],
                    },
                }
            ],
        )

        self.assertEqual(FailureCategory.WORKSPACE_VIOLATION, classification.category)
        self.assertIn("workspace", classification.reason)

    def test_classifies_loop_guard_from_hook_trace(self) -> None:
        classification = classify_failure(
            run_state={"status": "stopped"},
            events=[
                {
                    "event": "tool.call.completed",
                    "payload": {
                        "tool_name": "bash",
                        "status": "denied",
                        "pre_hook_trace": [
                            {
                                "hook_name": "tool_loop_guard",
                                "decision": "deny",
                                "reason": "Repeated tool call blocked",
                            }
                        ],
                    },
                }
            ],
        )

        self.assertEqual(FailureCategory.LOOP_GUARD, classification.category)

    def test_classifies_docker_error_from_external_log(self) -> None:
        classification = classify_failure(
            run_state={"status": "failed"},
            external_logs=[
                'docker.errors.APIError: Get "https://registry-1.docker.io/v2/": context deadline exceeded'
            ],
        )

        self.assertEqual(FailureCategory.DOCKER_ERROR, classification.category)
        self.assertTrue(classification.evidence)

    def test_trace_store_writes_trace_summary_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace_store = TraceStore(Path(tmp) / ".runs")
            run_state = RunState.create(session_id="test:trace", metadata={"benchmark_task_id": "task-1"})
            trace_store.start_run(run_state)
            trace_store.append_event(run_state, "workspace.resolved", {
                "workspace_root": "/tmp/workspace",
                "workspace_allowed_root": "/tmp",
                "workspace_requested": "/tmp/workspace",
            })
            run_state.finish_success("done")
            trace_store.write_run_state(run_state)
            trace_store.write_report(run_state, {
                "benchmark_task_id": "task-1",
                "workspace_root": "/tmp/workspace",
            })

            run_dir = trace_store.run_dir(run_state)
            summary_json = json.loads((run_dir / "trace_summary.json").read_text(encoding="utf-8"))
            summary_md = (run_dir / "trace_summary.md").read_text(encoding="utf-8")

            self.assertEqual("task-1", summary_json["task_id"])
            self.assertEqual("pass", summary_json["failure"]["category"])
            self.assertEqual("/tmp/workspace", summary_json["workspace"]["resolved"])
            self.assertTrue(summary_json["execution_path"])
            self.assertIn("执行路径", summary_md)
            self.assertIn("Trace Summary", summary_md)


if __name__ == "__main__":
    unittest.main()
