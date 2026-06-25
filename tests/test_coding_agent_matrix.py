import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.run_coding_agent_matrix import expand_cells


class CodingAgentMatrixTests(unittest.TestCase):
    def test_expand_cells_crosses_agents_features_tasks_and_repetitions(self) -> None:
        config = {
            "schema_version": 1,
            "benchmark_path": "benchmarks/coding_tasks.json",
            "task_ids": ["one", "two"],
            "repetitions": 2,
            "agents": [
                {"name": "a", "runner": "scripted"},
                {"name": "disabled", "enabled": False, "runner": "real"},
            ],
            "feature_sets": [
                {"name": "budget-on", "env": {"CONTEXT_ENABLE_SECTION_BUDGET": "1"}},
                {"name": "budget-off", "env": {"CONTEXT_ENABLE_SECTION_BUDGET": "0"}},
            ],
        }

        cells = expand_cells(config)

        self.assertEqual(8, len(cells))
        self.assertEqual(8, len({cell["cell_id"] for cell in cells}))
        self.assertEqual({"one", "two"}, {cell["task_id"] for cell in cells})
        self.assertEqual({"budget-on", "budget-off"}, {cell["feature_set_name"] for cell in cells})
        self.assertTrue(all(cell["runner"] == "scripted" for cell in cells))

    def test_matrix_script_runs_one_scripted_cell_and_writes_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "matrix.json"
            output_root = root / "matrix-runs"
            config_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "benchmark_path": "benchmarks/coding_tasks.json",
                        "task_ids": ["coding-git-diff-008"],
                        "agents": [{"name": "scripted", "runner": "scripted"}],
                        "feature_sets": [
                            {
                                "name": "budget-on",
                                "env": {"CONTEXT_ENABLE_SECTION_BUDGET": "1"},
                            }
                        ],
                        "metrics": [
                            "pass_rate",
                            "total_tokens",
                            "wall_duration_ms",
                            "tool_calls",
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/run_coding_agent_matrix.py",
                    "--config",
                    str(config_path),
                    "--output-root",
                    str(output_root),
                ],
                cwd=Path(__file__).resolve().parent.parent,
                capture_output=True,
                text=True,
                check=True,
            )

            self.assertIn("wrote matrix report:", result.stdout)
            self.assertIn("PASS coding-git-diff-008", result.stdout)
            matrix_dirs = sorted(output_root.glob("matrix_*"))
            self.assertEqual(1, len(matrix_dirs))
            matrix_dir = matrix_dirs[0]
            self.assertTrue((matrix_dir / "report.md").exists())
            self.assertTrue((matrix_dir / "report.json").exists())
            self.assertTrue((matrix_dir / "rows.csv").exists())
            self.assertTrue((matrix_dir / "task_rows.csv").exists())

            report = json.loads((matrix_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(1, report["summary"]["cells"])
            self.assertEqual(1, report["summary"]["passed_cells"])
            self.assertEqual(0, report["summary"]["error_cells"])
            self.assertEqual("pass", report["cells"][0]["status"])
            self.assertEqual(1.0, report["cells"][0]["metrics"]["pass_rate"])
            self.assertIn("tool_calls", report["cells"][0]["metrics"])
            self.assertEqual("coding-git-diff-008", report["cells"][0]["tasks"][0]["id"])


if __name__ == "__main__":
    unittest.main()
