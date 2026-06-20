from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from evaluation.swebench_adapter import (
    SweBenchInstance,
    build_swebench_prompt,
    official_evaluation_command,
    prediction_record,
    repo_clone_url,
    safe_instance_dir,
    write_prediction,
)


class SweBenchAdapterTests(unittest.TestCase):
    def test_build_prompt_contains_task_context_without_absolute_workspace(self) -> None:
        instance = SweBenchInstance(
            instance_id="sympy__sympy-20590",
            repo="sympy/sympy",
            base_commit="abc123",
            problem_statement="Fix simplify for nested powers.",
            hints_text="Look near pow handling.",
        )
        prompt = build_swebench_prompt(instance)

        self.assertIn("Instance ID: sympy__sympy-20590", prompt)
        self.assertIn("Repository: sympy/sympy", prompt)
        self.assertIn("Base commit: abc123", prompt)
        self.assertIn("Fix simplify for nested powers.", prompt)
        self.assertIn("Use relative paths", prompt)
        self.assertIn("Look near pow handling.", prompt)

    def test_prediction_record_matches_official_jsonl_shape(self) -> None:
        record = prediction_record(
            instance_id="django__django-12345",
            model_name_or_path="codex-local",
            model_patch="diff --git a/foo.py b/foo.py\n",
        )

        self.assertEqual(
            set(record),
            {"instance_id", "model_name_or_path", "model_patch"},
        )
        self.assertEqual(record["instance_id"], "django__django-12345")

    def test_write_prediction_writes_one_json_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "predictions.jsonl"
            write_prediction(
                path,
                instance_id="pytest__pytest-111",
                model_name_or_path="codex-local",
                model_patch="diff --git a/a b/a\n",
            )

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertEqual(payload["instance_id"], "pytest__pytest-111")
            self.assertIn("model_patch", payload)

    def test_repo_clone_url_and_safe_dir(self) -> None:
        self.assertEqual(
            repo_clone_url("psf/requests"),
            "https://github.com/psf/requests.git",
        )
        self.assertEqual(repo_clone_url("https://example.test/repo.git"), "https://example.test/repo.git")
        self.assertEqual(safe_instance_dir("a/b c"), "a_b_c")
        with self.assertRaises(ValueError):
            repo_clone_url("../bad")

    def test_official_eval_command_includes_instance_filter(self) -> None:
        command = official_evaluation_command(
            swebench_repo="/tmp/SWE-bench",
            dataset_name="princeton-nlp/SWE-bench_Lite",
            predictions_path="/tmp/predictions.jsonl",
            run_id="run-1",
            instance_id="sympy__sympy-20590",
        )

        self.assertIn("swebench.harness.run_evaluation", command)
        self.assertIn("--predictions_path", command)
        self.assertIn("/tmp/predictions.jsonl", command)
        self.assertIn("--instance_ids", command)
        self.assertIn("sympy__sympy-20590", command)


if __name__ == "__main__":
    unittest.main()
