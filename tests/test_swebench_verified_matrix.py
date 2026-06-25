import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.run_swebench_verified_matrix import build_cell_result, expand_cells


class SweBenchVerifiedMatrixTests(unittest.TestCase):
    def test_expand_cells_crosses_agents_features_and_repetitions(self) -> None:
        config = {
            "schema_version": 1,
            "swebench": {"limit": 1},
            "repetitions": 2,
            "agents": [
                {"name": "a", "env": {"LLM_ROUTE_CODING": "one"}},
                {"name": "disabled", "enabled": False},
            ],
            "feature_sets": [
                {"name": "budget-on", "env": {"CONTEXT_ENABLE_SECTION_BUDGET": "1"}},
                {"name": "budget-off", "env": {"CONTEXT_ENABLE_SECTION_BUDGET": "0"}},
            ],
        }

        cells = expand_cells(config)

        self.assertEqual(4, len(cells))
        self.assertEqual(4, len({cell["cell_id"] for cell in cells}))
        self.assertEqual({"a"}, {cell["agent_name"] for cell in cells})
        self.assertEqual({"budget-on", "budget-off"}, {cell["feature_set_name"] for cell in cells})
        self.assertEqual({1, 2}, {cell["repetition"] for cell in cells})

    def test_build_cell_result_summarizes_verified_rows(self) -> None:
        cell = {
            "cell_id": "real-default__budget-on",
            "agent_name": "real-default",
            "feature_set_name": "budget-on",
            "dimensions": {"context_budget": "on"},
        }
        batch_payload = {
            "batch_id": "swe_verified_test",
            "batch_dir": "/tmp/batch",
            "predictions_path": "/tmp/predictions.jsonl",
            "dataset": {
                "resolved_name": "SWE-bench/SWE-bench_Verified",
                "split": "test",
                "selected_count": 2,
            },
            "summary": {
                "total": 2,
                "agent_passed": 1,
                "agent_failed": 1,
                "errors": 0,
                "agent_pass_rate": 0.5,
                "patches_written": 2,
            },
            "rows": [
                {
                    "instance_id": "repo__repo-1",
                    "repo": "owner/repo",
                    "status": "pass",
                    "duration_ms": 100,
                    "patch_bytes": 10,
                    "metrics": {
                        "total_tokens": 1000,
                        "input_tokens": 700,
                        "output_tokens": 300,
                        "tool_calls": 4,
                        "model_calls": 2,
                    },
                },
                {
                    "instance_id": "repo__repo-2",
                    "repo": "owner/repo",
                    "status": "fail",
                    "error": "stopped",
                    "duration_ms": 200,
                    "patch_bytes": 20,
                    "metrics": {
                        "total_tokens": 2000,
                        "input_tokens": 1400,
                        "output_tokens": 600,
                        "tool_calls": 6,
                        "model_calls": 3,
                    },
                },
            ],
        }

        result = build_cell_result(
            cell,
            cell_dir=Path("/tmp/cell"),
            batch_payload=batch_payload,
            returncode=0,
            process_error="",
            wall_duration_ms=300,
        )

        self.assertEqual("fail", result["status"])
        self.assertEqual(0.5, result["metrics"]["pass_rate"])
        self.assertEqual(3000, result["metrics"]["total_tokens"])
        self.assertEqual(10, result["metrics"]["tool_calls"])
        self.assertEqual(1500, result["metrics"]["avg_total_tokens_per_instance"])
        self.assertEqual(2, len(result["tasks"]))
        self.assertEqual(["repo__repo-2"], [task["id"] for task in result["failed_tasks"]])

    def test_matrix_script_dry_run_writes_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "verified-matrix.json"
            output_root = root / "matrix-runs"
            config_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "swebench": {
                            "dataset_name": "verified",
                            "split": "test",
                            "limit": 1,
                            "offset": 0,
                        },
                        "agents": [{"name": "real-default"}],
                        "feature_sets": [
                            {
                                "name": "budget-on",
                                "env": {"CONTEXT_ENABLE_SECTION_BUDGET": "1"},
                            }
                        ],
                        "metrics": ["pass_rate", "total_tokens", "tool_calls"],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/run_swebench_verified_matrix.py",
                    "--config",
                    str(config_path),
                    "--output-root",
                    str(output_root),
                    "--dry-run",
                ],
                cwd=Path(__file__).resolve().parent.parent,
                capture_output=True,
                text=True,
                check=True,
            )

            self.assertIn("wrote SWE-bench Verified matrix report:", result.stdout)
            matrix_dirs = sorted(output_root.glob("swe_verified_matrix_*"))
            self.assertEqual(1, len(matrix_dirs))
            matrix_dir = matrix_dirs[0]
            self.assertTrue((matrix_dir / "report.md").exists())
            self.assertTrue((matrix_dir / "report.json").exists())
            self.assertTrue((matrix_dir / "rows.csv").exists())
            self.assertTrue((matrix_dir / "task_rows.csv").exists())
            self.assertTrue((matrix_dir / "expanded_plan.json").exists())

            report = json.loads((matrix_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(1, report["summary"]["cells"])
            self.assertEqual(1, report["summary"]["planned_cells"])
            self.assertEqual("planned", report["cells"][0]["status"])


if __name__ == "__main__":
    unittest.main()
