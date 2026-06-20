import json
import tempfile
import unittest
from pathlib import Path

from evaluation.harness import _tool_registry_for, diagnose_failure, run_coding_benchmark
from evaluation.task_schema import load_benchmark
from sessions.session import Session


class CodingBenchmarkTests(unittest.TestCase):
    def test_coding_benchmark_schema_has_pico_sized_task_set(self) -> None:
        tasks = load_benchmark(Path("benchmarks/coding_tasks.json"))

        self.assertEqual(12, len(tasks))
        self.assertEqual({
            "bugfix",
            "feature",
            "refactor",
            "docs",
            "tool-boundary",
            "safety",
            "git",
            "recovery",
            "loop-guard",
            "observability",
            "memory",
        }, {task.category for task in tasks})

    def test_coding_benchmark_runs_all_tasks_and_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = run_coding_benchmark(
                benchmark_path=Path("benchmarks/coding_tasks.json"),
                eval_root=root / ".evals" / "runs",
                workspace_root=root / "workspaces",
            )

            self.assertEqual(12, payload["summary"]["total_tasks"])
            self.assertEqual(1.0, payload["summary"]["pass_rate"])
            self.assertEqual(1.0, payload["summary"]["verifier_pass_rate"])
            self.assertEqual(1.0, payload["summary"]["trace_completeness_rate"])
            self.assertEqual("scripted", payload["runner"]["mode"])

            eval_dir = root / ".evals" / "runs" / payload["eval_id"]
            self.assertTrue((eval_dir / "summary.json").exists())
            self.assertTrue((eval_dir / "rows.json").exists())
            self.assertTrue((eval_dir / "summary.md").exists())
            summary_markdown = (eval_dir / "summary.md").read_text(encoding="utf-8")
            self.assertIn("## Summary", summary_markdown)
            self.assertIn("Runner: `scripted`", summary_markdown)
            self.assertIn("## Failure Categories", summary_markdown)
            persisted = json.loads((eval_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"], persisted["summary"])

    def test_coding_benchmark_can_replay_one_task_and_keep_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = []
            payload = run_coding_benchmark(
                benchmark_path=Path("benchmarks/coding_tasks.json"),
                eval_root=root / ".evals" / "runs",
                workspace_root=root / "workspaces",
                task_id="coding-git-diff-008",
                keep_workspace=True,
                progress=events.append,
            )

            self.assertEqual(1, payload["summary"]["total_tasks"])
            self.assertEqual("coding-git-diff-008", payload["benchmark"]["task_id"])
            self.assertTrue(payload["workspace_retained"])
            workspace = Path(payload["rows"][0]["workspace_path"])
            self.assertTrue(workspace.exists())
            self.assertIn("0.2.0", (workspace / "src" / "version.py").read_text(encoding="utf-8"))
            self.assertEqual(
                ["eval_started", "task_started", "task_finished", "eval_finished"],
                [event["event"] for event in events],
            )

    def test_failure_diagnosis_names_failed_check(self) -> None:
        row = {
            "run_status": "completed",
            "within_budget": True,
            "verifier_passed": False,
            "workspace_diff_passed": False,
            "trace_passed": True,
            "verifier": {
                "checks": [
                    {"name": "not_modified", "path": "tests/test_example.py", "passed": False}
                ]
            },
        }

        self.assertEqual("unexpected_file_modified", diagnose_failure(row))

    def test_failure_diagnosis_detects_budget_stop(self) -> None:
        row = {
            "run_status": "completed",
            "within_budget": True,
            "verifier_passed": False,
            "workspace_diff_passed": False,
            "trace_passed": False,
            "final_answer": "本轮已停止：工具推理步骤超过上限 (7)，已触发循环保护。",
            "verifier": {"checks": []},
        }

        self.assertEqual("budget_stop", diagnose_failure(row))

    def test_benchmark_allowed_write_tools_are_visible_without_unlock(self) -> None:
        registry = _tool_registry_for(["list_files", "read_file", "edit_file", "git_diff"])
        session = Session(id="task:benchmark", current_mode="coding")

        visible = registry.visible_names_for_turn(session, "coding")

        self.assertIn("edit_file", visible)
        self.assertIn("list_files", visible)


if __name__ == "__main__":
    unittest.main()
