from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from evaluation.swebench_adapter import (
    SweBenchInstance,
    build_swebench_prompt,
    clone_swebench_repo,
    dataset_name_candidates,
    load_swebench_instances,
    load_swebench_records_from_file,
    official_evaluation_command,
    prediction_record,
    repo_clone_url,
    safe_instance_dir,
    write_prediction,
    _run_command,
)
from unittest.mock import patch


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

    def test_verified_dataset_aliases_include_current_and_legacy_names(self) -> None:
        self.assertEqual(
            dataset_name_candidates("verified"),
            ["SWE-bench/SWE-bench_Verified", "princeton-nlp/SWE-bench_Verified"],
        )
        self.assertEqual(
            dataset_name_candidates("swe-verified"),
            ["SWE-bench/SWE-bench_Verified", "princeton-nlp/SWE-bench_Verified"],
        )

    def test_load_swebench_instances_can_select_limit_offset_or_explicit_ids(self) -> None:
        records = [
            {
                "instance_id": f"repo__repo-{index}",
                "repo": "owner/repo",
                "base_commit": f"abc{index}",
                "problem_statement": f"fix {index}",
            }
            for index in range(5)
        ]
        with patch("evaluation.swebench_adapter.load_swebench_records", return_value=records):
            sliced = load_swebench_instances(dataset_name="verified", split="test", limit=2, offset=1)
            explicit = load_swebench_instances(
                dataset_name="verified",
                split="test",
                instance_ids=["repo__repo-3", "repo__repo-1"],
            )

        self.assertEqual(["repo__repo-1", "repo__repo-2"], [item.instance_id for item in sliced])
        self.assertEqual(["repo__repo-3", "repo__repo-1"], [item.instance_id for item in explicit])

    def test_load_swebench_instances_can_use_local_records_file(self) -> None:
        records = [
            {
                "instance_id": f"repo__repo-{index}",
                "repo": "owner/repo",
                "base_commit": f"abc{index}",
                "problem_statement": f"fix {index}",
            }
            for index in range(3)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "selected_instances.json"
            path.write_text(json.dumps(records), encoding="utf-8")

            selected = load_swebench_instances(
                records_path=path,
                instance_ids=["repo__repo-2"],
            )

        self.assertEqual(["repo__repo-2"], [item.instance_id for item in selected])

    def test_load_swebench_records_from_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.jsonl"
            path.write_text(
                "\n".join([
                    json.dumps({
                        "instance_id": "repo__repo-1",
                        "repo": "owner/repo",
                        "base_commit": "abc1",
                        "problem_statement": "fix 1",
                    }),
                    json.dumps({
                        "instance_id": "repo__repo-2",
                        "repo": "owner/repo",
                        "base_commit": "abc2",
                        "problem_statement": "fix 2",
                    }),
                ]),
                encoding="utf-8",
            )

            records = load_swebench_records_from_file(path)

        self.assertEqual(["repo__repo-1", "repo__repo-2"], [record["instance_id"] for record in records])

    def test_official_eval_command_accepts_multiple_instance_ids(self) -> None:
        command = official_evaluation_command(
            swebench_repo="/tmp/SWE-bench",
            dataset_name="SWE-bench/SWE-bench_Verified",
            predictions_path="/tmp/predictions.jsonl",
            run_id="run-verified",
            instance_ids=["a__a-1", "b__b-2"],
        )

        index = command.index("--instance_ids")
        self.assertEqual(["a__a-1", "b__b-2"], command[index + 1 : index + 3])

    def test_clone_uses_bare_repo_cache_when_configured(self) -> None:
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("evaluation.swebench_adapter.subprocess.run", side_effect=fake_run):
                clone_swebench_repo(
                    "owner/repo",
                    root / "workspace",
                    repo_cache_root=root / "cache",
                    retries=1,
                )

        self.assertEqual(["git", "clone", "--mirror", "https://github.com/owner/repo.git"], calls[0][:4])
        self.assertEqual(["git", "clone"], calls[1][:2])
        self.assertIn("owner_repo.git", calls[1][2])

    def test_run_command_error_includes_stderr(self) -> None:
        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 128, "", "network dropped")

        with patch("evaluation.swebench_adapter.subprocess.run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                _run_command(["git", "clone", "bad"], cwd=Path("/tmp"))

        self.assertIn("network dropped", str(ctx.exception))
        self.assertIn("git clone bad", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
