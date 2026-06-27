import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class RunSweBenchVerifiedScriptTests(unittest.TestCase):
    def test_dry_run_can_select_instances_from_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records_path = root / "records.json"
            output_root = root / "runs"
            records_path.write_text(
                json.dumps([
                    {
                        "instance_id": "repo__repo-1",
                        "repo": "owner/repo",
                        "base_commit": "abc1",
                        "problem_statement": "fix one",
                    },
                    {
                        "instance_id": "repo__repo-2",
                        "repo": "owner/repo",
                        "base_commit": "abc2",
                        "problem_statement": "fix two",
                    },
                ]),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/run_swebench_verified.py",
                    "--instances-file",
                    str(records_path),
                    "--instance-id",
                    "repo__repo-2",
                    "--dry-run",
                    "--eval-root",
                    str(output_root),
                ],
                cwd=Path(__file__).resolve().parent.parent,
                capture_output=True,
                text=True,
                check=True,
            )

            self.assertIn("repo__repo-2", result.stdout)
            batch_dirs = sorted(output_root.glob("swe_verified_*"))
            self.assertEqual(1, len(batch_dirs))
            summary = json.loads((batch_dirs[0] / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(str(records_path), summary["dataset"]["instances_file"])
            selected = json.loads((batch_dirs[0] / "selected_instances.json").read_text(encoding="utf-8"))
            self.assertEqual(["repo__repo-2"], [item["instance_id"] for item in selected])


if __name__ == "__main__":
    unittest.main()
