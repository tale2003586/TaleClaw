import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class RunSystemEvalsScriptTests(unittest.TestCase):
    def test_compatibility_entrypoint_runs_one_scripted_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/run_system_evals.py",
                    "--task-id",
                    "coding-git-diff-008",
                    "--output-root",
                    str(root / ".evals" / "runs"),
                    "--workspace-root",
                    str(root / "workspaces"),
                    "--quiet",
                ],
                cwd=Path(__file__).resolve().parent.parent,
                capture_output=True,
                text=True,
                check=True,
            )

            self.assertIn("wrote eval run:", result.stdout)
            eval_dirs = sorted((root / ".evals" / "runs").glob("eval_*"))
            self.assertEqual(1, len(eval_dirs))
            rows = json.loads((eval_dirs[0] / "rows.json").read_text(encoding="utf-8"))
            summary = json.loads((eval_dirs[0] / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual("coding-git-diff-008", rows[0]["id"])
            self.assertEqual(1, summary["summary"]["total_tasks"])
            self.assertEqual(1.0, summary["summary"]["pass_rate"])


if __name__ == "__main__":
    unittest.main()
